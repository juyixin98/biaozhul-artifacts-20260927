"""Structured audit logging and request identity.

Every notable action is written to two sinks inside the same flow:

1. the ``audit_events`` SQLite table (via :mod:`secretscan.state`, inside the
   state transaction), and
2. the Python logging subsystem as a single structured JSON line.

Request identity is carried by :class:`RequestContext` — ``X-Request-Id`` for
correlation and ``X-Actor-Id`` for the acting principal. Both appear in every
log line and audit row. The logging formatter never renders a raw secret:
:class:`~secretscan.security.RedactingFilter` strips any value registered for
the current scan, and structured payloads only ever contain masks.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .security import RedactingFilter

# Context vars let deep helper code attribute log lines without threading args.
_current_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "secretscan_request_id", default=None)
_current_actor_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "secretscan_actor_id", default=None)

DEFAULT_ACTOR = "local-cli"

# Audit action vocabulary.
ACT_SCAN_STARTED = "scan.started"
ACT_SCAN_COMPLETED = "scan.completed"
ACT_SCAN_FAILED = "scan.failed"
ACT_FINDING_NEW = "finding.new"
ACT_FINDING_OPEN = "finding.open"
ACT_FINDING_MOVED = "finding.moved"
ACT_FINDING_EXEMPT = "finding.baseline_exempt"
ACT_FINDING_KNOWN_FIXED = "finding.known_fixed"
ACT_FINDING_UNCERTAIN = "finding.uncertain_removal"
ACT_BASELINE_LOADED = "baseline.loaded"
ACT_API_DENIED = "api.denied"

_OUTCOME_OK = "ok"
_OUTCOME_DENIED = "denied"
_OUTCOME_ERROR = "error"


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    actor_id: str

    @classmethod
    def create(cls, request_id: str | None = None,
               actor_id: str | None = None) -> "RequestContext":
        rid = (request_id or "").strip() or f"req-{uuid.uuid4().hex[:12]}"
        actor = (actor_id or "").strip() or DEFAULT_ACTOR
        return cls(request_id=rid, actor_id=actor)

    def __enter__(self) -> "RequestContext":
        object.__setattr__(
            self, "_rid_token", _current_request_id.set(self.request_id))
        object.__setattr__(
            self, "_actor_token", _current_actor_id.set(self.actor_id))
        return self

    def __exit__(self, *exc: Any) -> None:
        _current_request_id.reset(self._rid_token)
        _current_actor_id.reset(self._actor_token)


def current_request_id() -> str | None:
    return _current_request_id.get()


def current_actor_id() -> str | None:
    return _current_actor_id.get()


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with request identity and failure fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": _current_request_id.get(),
            "actor_id": _current_actor_id.get(),
        }
        # Structured event fields live in ``extra={"event": {...}}``.
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            for key in ("action", "target_type", "target", "outcome",
                        "scan_id", "reason_code"):
                if key in event:
                    payload[key] = event[key]
            for key, value in event.items():
                if key not in payload:
                    payload[key] = value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(log_file: str | Path | None = None,
                      level: int = logging.INFO) -> tuple[logging.Logger,
                                                          RedactingFilter]:
    """Configure the package logger with redaction and JSON formatting."""
    logger = logging.getLogger("secretscan")
    logger.setLevel(level)
    logger.handlers.clear()
    redactor = RedactingFilter()
    formatter = JsonFormatter()
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.addFilter(redactor)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.addFilter(redactor)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    logger.propagate = False
    return logger, redactor


def audit_event(logger: logging.Logger, *, action: str, target_type: str,
                target: str, outcome: str = _OUTCOME_OK,
                scan_id: int | None = None, **details: Any) -> None:
    """Emit one structured audit log line (the DB row is written separately)."""
    logger.info(
        action,
        extra={"event": {
            "action": action, "target_type": target_type, "target": target,
            "outcome": outcome, "scan_id": scan_id, **details,
        }})
