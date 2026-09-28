"""Structured logging helpers.

Every record carries a run/correlation id so a test log line can be tied back to
the input or run that produced it. Records also carry an explicit ``step`` and
``verdict`` field instead of folding exceptions into a generic success.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
from typing import Any, Iterator

import contextlib

_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")


def set_run_id(run_id: str) -> contextvars.Token[str]:
    return _run_id_var.set(run_id)


def reset_run_id(token: contextvars.Token[str]) -> None:
    _run_id_var.reset(token)


@contextlib.contextmanager
def run_context(run_id: str) -> Iterator[None]:
    token = set_run_id(run_id)
    try:
        yield
    finally:
        reset_run_id(token)


def get_run_id() -> str:
    return _run_id_var.get()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "run_id": getattr(record, "run_id", _run_id_var.get()),
            "component": record.name,
            "event": record.getMessage(),
        }
        for key in ("step", "verdict", "type", "offset", "length", "limit", "reason", "detail"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, default=str)


_CONFIGURED = False


def configure_logging(level: str | None = None) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("abibackend")
    if _CONFIGURED:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(level or os.environ.get("ABI_LOG_LEVEL", "INFO"))
    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(component: str) -> logging.Logger:
    logger = configure_logging()
    return logger.getChild(component)
