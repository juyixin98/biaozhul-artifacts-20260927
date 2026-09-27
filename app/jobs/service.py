"""Job execution layer: bridges HTTP jobs, the pipeline and the SQLite store."""
from __future__ import annotations

import hashlib
import traceback
import uuid

from ..config import Settings
from ..logging_setup import get_logger
from ..services.pipeline import run_validation
from .store import JobStore

log = get_logger("jobs.service")

# Pipeline status -> terminal job status.
_STATUS_MAP = {
    "clean": "succeeded",
    "repaired": "succeeded",
    "parse_failed": "parse_failed",
    "infeasible_bounds": "failed",
    "budget_exceeded": "failed",
    "solver_too_large": "failed",
}


class JobService:
    def __init__(self, store: JobStore, settings: Settings):
        self._store = store
        self._settings = settings

    def submit(self, data: bytes, fmt: str | None) -> str:
        job_id = f"job-{uuid.uuid4().hex}"
        sha = hashlib.sha256(data).hexdigest()
        self._store.create(job_id, data, fmt, sha)
        self._run(job_id, data, fmt)
        return job_id

    def _run(self, job_id: str, data: bytes, fmt: str | None) -> None:
        self._store.set_running(job_id)
        try:
            result = run_validation(
                data, settings=self._settings, fmt=fmt, run_id=job_id
            )
        except Exception as exc:  # never present exceptions as success
            tb = traceback.format_exc(limit=5)
            log.exception("[%s] unexpected pipeline error", job_id)
            self._store.fail_exception(job_id, f"{type(exc).__name__}: {exc}\n{tb}")
            return
        body = self._serialize_result(result)
        terminal = _STATUS_MAP.get(result.status)
        if terminal is None:
            self._store.fail_exception(
                job_id, f"unknown pipeline status {result.status!r}"
            )
            return
        self._store.finish(
            job_id,
            status=terminal,
            fmt=result.fmt,
            cue_count=result.cue_count,
            result=body,
            repaired_document=result.repaired_document,
        )

    @staticmethod
    def _serialize_result(result) -> dict:
        return {
            "run_id": result.run_id,
            "status": result.status,
            "format": result.fmt,
            "cue_count": result.cue_count,
            "message": result.message,
            "elapsed_ms": round(result.elapsed_ms, 3),
            "failure_codes": result.failure_codes,
            "diagnostics": [
                {
                    "code": d.code,
                    "severity": d.severity.value if hasattr(d.severity, "value")
                    else str(d.severity),
                    "message": d.message,
                    "cue_index": d.cue_index,
                    "other_index": d.other_index,
                    "detail": d.detail,
                }
                for d in result.diagnostics
            ],
            "repair": result.repair,
        }
