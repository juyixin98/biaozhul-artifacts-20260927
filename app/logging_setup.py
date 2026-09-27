"""Structured, request-correlated logging.

Emits one JSON object per event on stdout so logs can be grepped or ingested.
Every log line for a request carries the same ``request_id`` and, when known,
``job_id``; failures are emitted under ``event=request_failed`` with the stable
error code, and uncertain/partial results under ``event=partial_result``.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_job_id: ContextVar[str] = ContextVar("job_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "request_id": _request_id.get(),
            "job_id": _job_id.get(),
            "event": getattr(record, "event", "log"),
        }
        if isinstance(record.msg, dict):
            payload.update(record.msg)
        else:
            payload["message"] = record.getMessage()
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("r128")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(level)
    return logger


logger = configure_logging()


def set_request_context(request_id: str, job_id: str = "-") -> None:
    _request_id.set(request_id)
    _job_id.set(job_id)
