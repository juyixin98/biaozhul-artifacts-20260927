"""Structured logging helpers.

Every review log line carries the request id and the verdict/codes, and bound
values are passed through :mod:`redaction` — the raw values are never logged.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from .core.redaction import redact_bindings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "verdict", "codes", "stmt", "params_redacted",
                    "chain_ok", "path"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("sqlguard")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
    return logger


def log_review(logger: logging.Logger, *, request_id: str, verdict: str,
               codes: dict[str, list[str]], stmt: str | None,
               params: Any) -> None:
    logger.info(
        "review complete",
        extra={
            "request_id": request_id,
            "verdict": verdict,
            "codes": codes,
            "stmt": stmt,
            "params_redacted": redact_bindings(params),
        })
