"""Diagnostics: request ids, structured JSON logging and value redaction.

Every acceptance/rejection/undecidable decision can be logged with:
  * a request id (honoring an inbound X-Request-ID if the client supplied one),
  * the key category / verdict,
  * enough tree state (roots, revision) to reproduce,
  * redacted sensitive material (only key prefixes and value lengths).
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import Any, Dict, Optional

REDACTED_PREFIX = 8  # hex chars = 4 bytes shown


def new_request_id() -> str:
    return uuid.uuid4().hex


def redact_key(key_hex: Optional[str]) -> Optional[str]:
    if key_hex is None:
        return None
    return f"{key_hex[:REDACTED_PREFIX]}…({len(key_hex)} hex chars)"


def redact_value(value_hex: Optional[str]) -> Dict[str, Any]:
    if value_hex is None:
        return {"present": False}
    return {"present": True, "byte_length": len(value_hex) // 2}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload["context"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "json") -> logging.Logger:
    logger = logging.getLogger("smt")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger


def event(logger: logging.Logger, level: int, message: str, request_id: str, **context: Any) -> None:
    ctx: Dict[str, Any] = {"request_id": request_id}
    ctx.update(context)
    logger.log(level, message, extra={"context": ctx})
