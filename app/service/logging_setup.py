"""Structured JSONL logging.

Every line carries a run_id/job_id when available so a failing test or API call
can be correlated from the log back to its exact input and computation steps.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("run_id", "job_id", "batch_id", "step", "progress",
                    "details", "versions"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, default=repr)


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("dicunify")
    if logger.handlers:
        logger.setLevel(level)
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonlFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("dicunify")
