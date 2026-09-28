"""Structured logging.

Every log line carries the request identity (when bound) so that interface
results and on-disk logs can be correlated. Two formatters are provided:
JSON (default, machine readable) and a compact human formatter.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


def bind_request_id(request_id: str) -> contextvars.Token:
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": _request_id.get(),
            "message": record.getMessage(),
        }
        # Structured extras attached via logger.info(..., extra={"context": {...}})
        ctx = getattr(record, "context", None)
        if isinstance(ctx, dict):
            for key, value in ctx.items():
                if key not in payload:
                    payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rid = _request_id.get() or "-"
        head = f"{self.formatTime(record)} {record.levelname:<5} [{rid}] {record.name}: {record.getMessage()}"
        if record.exc_info:
            head += "\n" + self.formatException(record.exc_info)
        return head


_CONFIGURED = False


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    if json_output:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_HumanFormatter(datefmt="%H:%M:%S"))
    root = logging.getLogger("pnv")
    root.setLevel(level.upper())
    root.handlers[:] = [handler]
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"pnv.{name}")
