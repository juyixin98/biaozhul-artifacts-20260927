"""Application service: tie schema admission, kernel, adapters and metadata.

This is the transactional boundary used by the HTTP layer. Every request is
assigned/echoed with a request id; its evidence steps are persisted to SQLite
in one run transaction and mirrored to a structured logger that always shows
the request id, phase and location (page/slot/record) on failure.
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from ..core.errors import ErrorCode, Severity, StructuredError
from ..core.schema import Schema
from ..core.verifier import verify
from ..metadata.store import MetadataStore

log = logging.getLogger("parquet_verifier")


class VerificationService:
    def __init__(self, store: Optional[MetadataStore] = None,
                 workroot: Optional[Path] = None):
        self.store = store or MetadataStore()
        self.workroot = Path(workroot or tempfile.mkdtemp(prefix="pv-"))
        self.workroot.mkdir(parents=True, exist_ok=True)

    def admit_schema(self, raw_schema: dict[str, Any]) -> Schema:
        return Schema(raw_schema, max_nodes=settings.max_schema_nodes)

    def run_verification(self, raw_schema: dict[str, Any],
                        records: list[dict[str, Any]],
                        request_id: Optional[str] = None,
                        expected: Optional[list[dict[str, Any]]] = None,
                        page_slot_target: Optional[int] = None,
                        parquet_page_bytes: Optional[int] = None,
                        page_version: Optional[str] = None) -> dict[str, Any]:
        request_id = request_id or f"req-{uuid.uuid4().hex[:12]}"
        target = page_slot_target or settings.page_slot_target
        page_bytes = parquet_page_bytes or 256
        version = page_version or settings.parquet_page_version
        logger = _RequestLogger(request_id)

        # 0) schema admission
        try:
            schema = self.admit_schema(raw_schema)
        except StructuredError as exc:
            logger.failed("schema_admission", exc)
            self.store.create_run(request_id, raw_schema, len(records), version)
            self.store.append_event(request_id, 0, "schema_admission",
                                    "FAIL", exc.to_dict())
            self.store.finish_run(request_id, "FAILED", exc.to_dict())
            raise
        leaf_info = schema.describe_for_headers()
        logger.event("schema_admission", "PASS",
                     {"leaves": [li["path"] for li in leaf_info],
                      "leaf_count": len(leaf_info)})

        workdir = self.workroot / request_id
        workdir.mkdir(parents=True, exist_ok=True)
        self.store.create_run(request_id, raw_schema, len(records), version)
        self.store.append_event(
            request_id, 0, "schema_admission", "PASS",
            {"leaf_columns": leaf_info, "record_count": len(records)},
        )

        try:
            if len(records) > settings.max_records:
                raise StructuredError(
                    ErrorCode.INVALID_REQUEST,
                    f"too many records: {len(records)} > {settings.max_records}",
                    {"count": len(records), "limit": settings.max_records},
                    Severity.FATAL,
                )
            result = verify(
                schema, records, workdir=workdir,
                page_slot_target=target, page_version=version,
                expected=expected, parquet_page_bytes=page_bytes,
            )
        except StructuredError as exc:
            logger.failed("verification", exc)
            self.store.append_event(request_id, 1, "verification", "FAIL",
                                    exc.to_dict())
            self.store.finish_run(request_id, "FAILED", exc.to_dict())
            raise

        seq = 1
        for step in result["steps"]:
            self.store.append_event(request_id, seq, step["step"],
                                    step["status"], step)
            logger.event(step["step"], step["status"],
                         {k: v for k, v in step.items()
                          if k not in ("step", "status")})
            seq += 1
        for f in result["findings"]:
            sev = f["severity"]
            self.store.append_event(request_id, seq, f"finding:{f['code']}",
                                    sev, f)
            logger.event("finding", sev, f)
            seq += 1

        self.store.finish_run(request_id, result["status"])
        result["request_id"] = request_id
        if result["status"] == "FAILED":
            logger.failed_result(result)
        else:
            logger.event("run_complete", result["status"],
                         {"fatal": sum(1 for f in result["findings"]
                                       if f["severity"] == "FATAL"),
                          "uncertain": sum(1 for f in result["findings"]
                                           if f["severity"] == "UNCERTAIN")})
        return result

    def get_run(self, request_id: str) -> Optional[dict[str, Any]]:
        return self.store.get_run(request_id)


class _RequestLogger:
    """Structured logging that always carries the request identity."""

    def __init__(self, request_id: str):
        self.request_id = request_id

    def _log(self, level: int, phase: str, status: str,
             detail: dict[str, Any]) -> None:
        log.log(level, "verification event", extra={
            "request_id": self.request_id,
            "phase": phase,
            "status": status,
            "detail": detail,
        })

    def event(self, phase: str, status: str, detail: dict[str, Any]) -> None:
        level = logging.WARNING if status == "UNCERTAIN" else logging.INFO
        self._log(level, phase, status, detail)

    def failed(self, phase: str, exc: StructuredError) -> None:
        self._log(logging.ERROR, phase, "FAIL", exc.to_dict())

    def failed_result(self, result: dict[str, Any]) -> None:
        self._log(logging.ERROR, "verification", "FAILED",
                  {"findings": result["findings"]})
