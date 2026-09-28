"""Structured JSON logging correlated by run id and job id.

Every record carries the run identity, the job identity (when known), the
pipeline step and the dependency versions, so a log line can always be tied
back to the inputs and the code that produced it.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import dependency_versions

_RUN_ID: str | None = None


def get_run_id() -> str:
    """Process-wide run identity, stable for the lifetime of the process."""
    global _RUN_ID
    if _RUN_ID is None:
        _RUN_ID = uuid.uuid4().hex[:12]
    return _RUN_ID


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "run_id": getattr(record, "run_id", None),
            "job_id": getattr(record, "job_id", None),
            "step": getattr(record, "step", None),
            "event": record.getMessage(),
            "versions": getattr(record, "versions", None),
        }
        data = getattr(record, "data", None)
        if data is not None:
            payload["data"] = data
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ContextAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        for key, value in self.extra.items():
            extra.setdefault(key, value)
        extra.setdefault("versions", dependency_versions())
        return msg, kwargs

    def log_step(self, level: int, step: str, event: str,
                 data: dict[str, Any] | None = None) -> None:
        self.log(level, event, extra={"step": step, "data": data})


def get_logger(name: str, log_dir: str | Path, job_id: str | None = None,
               step: str | None = None) -> ContextAdapter:
    """A logger writing JSON lines both to the console and to
    <log_dir>/run-<run_id>.log."""
    run_id = get_run_id()
    logger = logging.getLogger(f"concat.{name}.{run_id}")
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        fmt = JsonFormatter()
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        logger.addHandler(console)
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(Path(log_dir) / f"run-{run_id}.log")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    return ContextAdapter(logger, {
        "run_id": run_id,
        "job_id": job_id,
        "step": step,
    })
