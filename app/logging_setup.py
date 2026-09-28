"""Structured logging with run identity correlation.

Every request/operation carries:
    run_id   - a unique execution id (generated if the caller did not supply one)
    input_fp - a fingerprint (sha256 prefix) of the raw input payload

so a test log line can be tied back to the exact input bytes and run. Logs are
rendered as single-line JSON when ``log_format == "prod"``.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import sys
import uuid
from contextlib import contextmanager
from typing import Iterator

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
input_fp_var: contextvars.ContextVar[str] = contextvars.ContextVar("input_fp", default="-")


def fingerprint(raw: bytes | bytearray | memoryview | str, length: int = 12) -> str:
    """Return a short stable fingerprint for input correlation."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(bytes(raw)).hexdigest()[:length]


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


@contextmanager
def bind_context(run_id: str | None = None, input_fp: str | None = None) -> Iterator[dict]:
    tokens = []
    info = {
        "run_id": run_id or new_run_id(),
        "input_fp": input_fp or "-",
    }
    tokens.append(run_id_var.set(info["run_id"]))
    tokens.append(input_fp_var.set(info["input_fp"]))
    try:
        yield info
    finally:
        for var, token in zip((run_id_var, input_fp_var), tokens):
            var.reset(token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        record.input_fp = input_fp_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "input_fp": getattr(record, "input_fp", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_CONFIGURED = False


def configure_logging(level: str = "INFO", fmt: str = "dev") -> None:
    global _CONFIGURED
    root = logging.getLogger("app")
    if _CONFIGURED:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_ContextFilter())
    if fmt == "prod":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s [run=%(run_id)s fp=%(input_fp)s] %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("app") else f"app.{name}")
