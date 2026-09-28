"""Structured JSON logging with request-id correlation.

Every log line is one JSON object carrying ``request_id`` (set by the API
middleware via :class:`RequestIdFilter`), level, logger name, event and the
step/version/location fields the caller passes.  Failures and uncertain
conclusions are emitted as their own events, never folded into an OK line.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def get_request_id() -> str:
    return _request_id.get()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", None) or _request_id.get(),
            "event": record.getMessage(),
        }
        for key in ("step", "location", "version", "detail", "uncertain"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: str = "INFO", file_path: str | None = None) -> None:
    root = logging.getLogger("zcluster")
    root.handlers.clear()
    root.setLevel(level.upper())
    root.propagate = False

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(_JsonFormatter())
    root.addHandler(stderr)

    if file_path:
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        fh = logging.FileHandler(file_path, encoding="utf-8")
        fh.setFormatter(_JsonFormatter())
        root.addHandler(fh)


def get_logger(name: str = "zcluster") -> logging.Logger:
    return logging.getLogger(name if name.startswith("zcluster") else f"zcluster.{name}")
