"""Structured run logging.

One logger per run. Each event is:
  * appended as a JSON line to the run's ``run.log`` (correlates by run id and
    input hash), and
  * mirrored to the process logger (stderr) at the matching level, and
  * mirrored into the HMAC-chained SQLite audit table.

Every log line includes the service version and the *criterion* behind a
decision (stage/outcome/category/evidence), satisfying the traceability
requirement: progress and judgment basis are visible, not just a final verdict.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from .audit.audit import AuditDB, utc_now_iso


class RunLogger:
    def __init__(
        self,
        run_id: str,
        version: str,
        log_path: Path,
        audit: AuditDB,
        *,
        input_sha256: str | None = None,
        filename: str | None = None,
    ):
        self.run_id = run_id
        self.version = version
        self.log_path = Path(log_path)
        self.audit = audit
        self.input_sha256 = input_sha256
        self.filename = filename
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.log_path, "a", encoding="utf-8")
        self._console = logging.getLogger(f"archiveguard.run.{run_id}")
        if not self._console.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s [run=%(run_id)s] %(message)s")
            )
            self._console.addHandler(handler)
            self._console.setLevel(logging.INFO)
        self._console.propagate = False
        self._extra = {"run_id": run_id}

    def _emit(self, level: int, stage: str, outcome: str, message: str, **fields: Any) -> dict:
        record = {
            "ts": utc_now_iso(),
            "run_id": self.run_id,
            "version": self.version,
            "input_sha256": self.input_sha256,
            "filename": self.filename,
            "stage": stage,
            "outcome": outcome,
            "message": message,
        }
        record.update(fields)
        self._fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._console.log(
            level,
            "%s/%s %s %s",
            stage,
            outcome,
            message,
            " ".join(f"{k}={v}" for k, v in fields.items() if k not in {"category", "evidence", "detail"}),
            extra=self._extra,
        )
        event = self.audit.record_event(
            self.run_id,
            stage,
            outcome,
            category=fields.get("category"),
            evidence=fields.get("evidence"),
            message=message,
            detail=fields.get("detail"),
        )
        return event

    def progress(self, stage: str, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, stage, "progress", message, **fields)

    def accepted(self, stage: str, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, stage, "accepted", message, **fields)

    def rejected(self, stage: str, category: str, message: str, *, evidence: str | None = None, **fields) -> None:
        self._emit(
            logging.WARNING, stage, "rejected", message,
            category=category, evidence=evidence, detail=fields or None,
        )

    def failed(self, stage: str, message: str, **fields: Any) -> None:
        self._emit(logging.ERROR, stage, "failed", message, **fields)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
