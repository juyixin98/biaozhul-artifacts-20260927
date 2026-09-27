"""结构化日志。

每行一条 JSON，统一带 request_id、事件类型、版本、关键步骤位置等，
使失败复现时“接口结果与日志可解释”，可以按请求身份完整回放。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, Optional

from .request_context import get_request_id

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "request_id": getattr(record, "request_id", None) or get_request_id(),
            "event": getattr(record, "event", record.getMessage()),
        }
        for key in (
            "query",
            "version",
            "node",
            "stage",
            "detail",
            "category",
            "failure_reason",
            "uncertainty",
            "result_count",
            "stats",
            "path",
            "method",
            "status_code",
            "elapsed_ms",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_path: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("posting_service")
    if _CONFIGURED:
        return logger
    logger.setLevel(level)
    logger.propagate = False

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(_JsonFormatter())
    logger.addHandler(stream_handler)

    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(_JsonFormatter())
        logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger() -> logging.Logger:
    return configure_logging()


def log_event(level: int, event: str, **fields: Any) -> None:
    get_logger().log(level, event, extra={"event": event, **fields})
