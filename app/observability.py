"""Structured operational logging with run/request correlation.

Every log line is one JSON object containing the run id, so a test or server
log can be filtered back to the inputs and decisions of a single execution.
The verifier reports its actual verdict code; exceptions are logged at ERROR
and never rewritten as success.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

LOGGER_NAME = "audit_commitments"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": utc_now(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("run_id", "batch_id", "request_id", "step", "verdict", "detail"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


_configured: bool = False
_configured_path: str | None = None


def configure_logging(log_path: str | None = None, *, verbose: bool = False,
                      force: bool = False) -> logging.Logger:
    """Configure package logging.

    Idempotent for the same file path; passing a different path (or force=True)
    re-attaches handlers so tests can redirect to an isolated log file.
    """
    global _configured, _configured_path
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    if _configured and not force and log_path == _configured_path:
        return logger

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(_JsonFormatter())
    logger.addHandler(stream)

    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(_JsonFormatter())
        logger.addHandler(file_handler)

    logger.propagate = False
    _configured = True
    _configured_path = log_path
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
