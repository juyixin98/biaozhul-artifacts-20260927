"""Structured, request-correlated JSON logging.

Every log line is a single JSON object carrying at minimum:

* ``request_id``: the identity correlating all processing of one API request,
* ``component``: where it was produced (``api`` / ``kernel`` / ``replay`` /
  ``storage``) plus the protocol version,
* ``event`` / ``level`` / ``message``,
* structured ``context`` for key inputs and outcomes.

Failures and uncertain conclusions use distinct events (``block_rejected``,
``tx_invalid``, ``warning``) so they can be filtered apart from normal steps.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any, Optional

from ..params import PARAMS

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    rid = uuid.uuid4().hex
    _REQUEST_ID.set(rid)
    return rid


def set_request_id(rid: str) -> None:
    _REQUEST_ID.set(rid)


def get_request_id() -> str:
    return _REQUEST_ID.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "request_id": getattr(record, "request_id", None) or _REQUEST_ID.get(),
            "component": getattr(record, "component", record.name),
            "protocol_version": PARAMS.protocol_version,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if context:
            payload["context"] = context
        failures = getattr(record, "failures", None)
        if failures:
            payload["failures"] = failures
        warnings = getattr(record, "warnings", None)
        if warnings:
            payload["warnings"] = warnings
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("basefee")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


class StructuredLogger:
    """Thin wrapper that stamps component/request context onto every event."""

    def __init__(self, component: str, logger: Optional[logging.Logger] = None):
        self._log = logger or configure_logging()
        self.component = component

    def _emit(self, level: int, event: str, message: str = "", **fields) -> None:
        self._log.log(
            level,
            message or event,
            extra={
                "event": event,
                "component": self.component,
                "request_id": _REQUEST_ID.get(),
                "context": fields.get("context"),
                "failures": fields.get("failures"),
                "warnings": fields.get("warnings"),
            },
        )

    def step(self, event: str, message: str = "", **context) -> None:
        self._emit(logging.INFO, event, message, context=context or None)

    def warning(self, event: str, message: str, **warnings) -> None:
        self._emit(logging.WARNING, event, message, warnings=warnings or None)

    def failure(self, event: str, message: str, **failures) -> None:
        self._emit(logging.ERROR, event, message, failures=failures or None)
