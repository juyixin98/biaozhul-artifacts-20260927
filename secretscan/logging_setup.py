"""Structured, request-correlated, redacted JSON logging."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .redact import Redactor

_REDACTOR = Redactor()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": _REDACTOR.redact(record.getMessage()),
        }
        for key in ("request_id", "actor", "project_id", "scan_id", "step", "code"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = _REDACTOR.redact(self.formatException(record.exc_info))
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("secretscan")
    logger.setLevel(level)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("secretscan")
