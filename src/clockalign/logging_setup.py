"""Structured logging with request/job correlation.

Every log line is JSON and carries the service version plus whichever of
``request_id`` / ``job_id`` apply, so a user can trace a particular request
through parsing -> sync extraction -> robust fit -> resampling -> storage.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any

from . import __version__

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None)
_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "job_id", default=None)


def _json_default(obj):
    try:  # numpy scalars/arrays without importing numpy at module load
        import numpy as np
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except ImportError:  # pragma: no cover
        pass
    return repr(obj)


def _json_safe(obj):
    """Recursively coerce numpy scalar/array values into plain Python."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def bind(request_id: str | None = None, job_id: str | None = None) -> None:
    if request_id is not None:
        _request_id.set(request_id)
    if job_id is not None:
        _job_id.set(job_id)


def current_request_id() -> str | None:
    return _request_id.get()


def current_job_id() -> str | None:
    return _job_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": "clockalign",
            "version": __version__,
            "logger": record.name,
            "message": record.getMessage(),
        }
        rid = _request_id.get()
        jid = _job_id.get()
        if rid:
            payload["request_id"] = rid
        if jid:
            payload["job_id"] = jid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(_json_safe(extra))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          default=_json_default)


def configure_logging(level: int | str = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("clockalign")
    logger.setLevel(level)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str = "clockalign") -> logging.Logger:
    return logging.getLogger(name if name.startswith("clockalign")
                             else f"clockalign.{name}")
