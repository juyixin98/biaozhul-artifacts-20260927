"""结构化运行日志：JSON 行格式，携带 run_id 以便与测试/诊断记录对齐重放。"""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

run_id_var: ContextVar[str] = ContextVar("run_id", default="-")
path_var: ContextVar[str] = ContextVar("path", default="-")

_LOGGER_NAME = "textindex"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "run_id": getattr(record, "run_id", run_id_var.get()),
            "path": getattr(record, "path", path_var.get()),
            "event": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload["data"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if getattr(logger, "_configured", False):
        logger.setLevel(level)
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    logger._configured = True  # type: ignore[attr-defined]
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def log_event(event: str, level: int = logging.INFO, **fields: object) -> None:
    get_logger().log(level, event, extra={"extra_fields": fields})
