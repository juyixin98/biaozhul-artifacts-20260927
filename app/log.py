"""Structured JSON logging.

Every line is one JSON object carrying request identity, step, algorithm and
processing location (worker). Failures (``level`` >= warning) and uncertain
conclusions are emitted as their own lines so they can be filtered for.
"""

from __future__ import annotations

import json
import logging
import sys
import time

from .config import Settings


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, worker_id: str, algorithm_id: str):
        super().__init__()
        self.service = service
        self.worker_id = worker_id
        self.algorithm_id = algorithm_id

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": self.service,
            "worker_id": self.worker_id,
            "algorithm_id": self.algorithm_id,
            "step": getattr(record, "step", "-"),
            "request_id": getattr(record, "request_id", "-"),
            "job_id": getattr(record, "job_id", "-"),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if extra:
            payload["context"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def configure_logging(settings: Settings, level: int = logging.INFO) -> logging.Logger:
    global _configured
    logger = logging.getLogger("r128")
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter(
            settings.service_name, settings.worker_id, settings.algorithm_id))
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
        _configured = True
    return logger
