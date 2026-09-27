"""结构化日志：JSON 行，带请求身份（request_id）与作业身份（job_id）。

每条日志至少含::

    ts          ISO8601 UTC 时间戳
    level       日志级别
    event       事件名（关键步骤标识）
    request_id  请求关联身份（中间件注入）
    job_id      作业关联身份（执行器注入，可空）
    module      产生日志的模块/版本位置
    version     服务版本
    ...         事件字段
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

from app import __version__

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_job_id: ContextVar[str] = ContextVar("job_id", default="-")


def set_request_id(value: str) -> None:
    _request_id.set(value)


def set_job_id(value: str) -> None:
    _job_id.set(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": _request_id.get(),
            "job_id": _job_id.get(),
            "module": record.name,
            "version": __version__,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        head = (
            f"{self.formatTime(record)} {record.levelname:<5} "
            f"[{_request_id.get()}/{_job_id.get()}] {record.name}: {record.getMessage()}"
        )
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict) and extra:
            head += " " + " ".join(f"{k}={v}" for k, v in sorted(extra.items()))
        if record.exc_info:
            head += "\n" + self.formatException(record.exc_info)
        return head


def configure_logging(level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if as_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(PlainFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind_fields(**fields) -> "FieldsAdapter":
    return FieldsAdapter(fields)


class FieldsAdapter:
    """小工具：``log.info('event', extra={'fields': {...}})`` 的简写。"""

    def __init__(self, static: dict | None = None) -> None:
        self._static = static or {}

    def emit(self, level: int, logger: logging.Logger, event: str, **fields) -> None:
        merged = {**self._static, **fields}
        logger.log(level, event, extra={"fields": merged})

    def info(self, logger: logging.Logger, event: str, **fields) -> None:
        self.emit(logging.INFO, logger, event, **fields)

    def warning(self, logger: logging.Logger, event: str, **fields) -> None:
        self.emit(logging.WARNING, logger, event, **fields)

    def error(self, logger: logging.Logger, event: str, **fields) -> None:
        self.emit(logging.ERROR, logger, event, **fields)

    def debug(self, logger: logging.Logger, event: str, **fields) -> None:
        self.emit(logging.DEBUG, logger, event, **fields)
