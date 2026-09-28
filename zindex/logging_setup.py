"""Structured logging setup.

Every log line is a single JSON object on stderr (and, when configured, a log
file). ``request_id`` is propagated per-context so all steps of one request can
be correlated, and failures / uncertain conclusions are recorded under their
own fields instead of being hidden in free text.
"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def get_request_id() -> str:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": _request_id.get(),
            "event": record.getMessage(),
        }
        # Structured keyword arguments attached via logger.info(..., extra={...})
        for key, value in record.__dict__.get("data", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def configure_logging(log_file: str | Path | None = None, level: str = "INFO") -> None:
    global _configured
    root = logging.getLogger("zindex")
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    root.setLevel(level)
    root.propagate = False
    _configured = True


def get_logger(name: str = "zindex") -> logging.Logger:
    return logging.getLogger(name if name.startswith("zindex") else f"zindex.{name}")
