"""Structured, request-correlated diagnostics.

Every pruning run receives a ``Trace`` bound to a request id. Steps, versions,
locations, failures and uncertain conclusions are collected separately and also
emitted as one-line JSON log records. The API echoes the trace back under
``"trace"`` so an operator can correlate a response with server-side logs.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from . import values as V


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "table", "stage", "location", "versions",
                    "detail", "code"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, default=V.json_default, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("prune")
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(JsonFormatter())
        logger.addHandler(h)
    logger.setLevel(level)
    logger.propagate = False
    return logger


class Trace:
    def __init__(self, request_id: Optional[str] = None,
                 table: Optional[str] = None, logger: Optional[logging.Logger] = None):
        self.request_id = request_id or new_request_id()
        self.table = table
        self.log = logger or configure_logging()
        self.steps: List[Dict[str, Any]] = []

    def _emit(self, level: int, stage: str, message: str, **extra: Any) -> None:
        self.log.log(level, message, extra={
            "request_id": self.request_id, "table": self.table,
            "stage": stage, **extra})

    def step(self, stage: str, message: str, location: Optional[str] = None,
             **detail: Any) -> None:
        rec = {"stage": stage, "message": message, "location": location, **detail}
        self.steps.append(rec)
        self._emit(logging.INFO, stage, message, location=location, detail=detail)

    def failure(self, code: str, message: str, location: Optional[str] = None,
                **detail: Any) -> None:
        rec = {"stage": "failure", "code": code, "message": message,
               "location": location, **detail}
        self.steps.append(rec)
        self._emit(logging.ERROR, "failure", message, code=code,
                   location=location, detail=detail)

    def uncertain(self, code: str, message: str, location: Optional[str] = None,
                  **detail: Any) -> None:
        rec = {"stage": "uncertain", "code": code, "message": message,
               "location": location, **detail}
        self.steps.append(rec)
        self._emit(logging.WARNING, "uncertain", message, code=code,
                   location=location, detail=detail)

    def versions(self, mapping: Dict[str, str], location: str = "kernel") -> None:
        rec = {"stage": "versions", "versions": mapping, "location": location}
        self.steps.append(rec)
        self._emit(logging.INFO, "versions",
                   " ".join(f"{k}={v}" for k, v in mapping.items()),
                   location=location, versions=mapping)

    def public(self) -> List[Dict[str, Any]]:
        return self.steps
