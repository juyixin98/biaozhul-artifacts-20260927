"""结构化 JSON 行日志：每行可按 run_id / job_id / 输入标识关联。

字段固定包含：ts、level、event、run_id、job_id、source、version，
以及 progress / step / decision / basis / extra 等可复算字段。
未知异常以 level=error 落盘，绝不被吞掉并伪装为成功。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import __version__

_RUN_ID = os.environ.get("MEDIACONCAT_RUN_ID") or f"run-{uuid.uuid4().hex[:12]}"
_lock = threading.Lock()


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs % 1000):03d}Z",
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "run_id": getattr(record, "run_id", _RUN_ID),
            "job_id": getattr(record, "job_id", None),
            "source": getattr(record, "source", None),
            "version": __version__,
        }
        for key in ("step", "progress", "decision", "basis", "extra", "exc_type"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc_type"] = record.exc_info[1].__class__.__name__ if record.exc_info[1] else None
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def run_id() -> str:
    return _RUN_ID


def configure_logging(log_dir: str = "logs", level: str = "INFO") -> logging.Logger:
    """配置根 logger：stderr 人类可读 + 文件 JSONL（按 run 关联）。"""
    logger = logging.getLogger("mediaconcat")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(stream)

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(Path(log_dir) / f"{_RUN_ID}.jsonl", encoding="utf-8")
    fh.setFormatter(JsonLineFormatter())
    logger.addHandler(fh)
    return logger


def get_logger() -> logging.Logger:
    logger = logging.getLogger("mediaconcat")
    if not logger.handlers:
        configure_logging()
    return logger


def bind(**fields: Any) -> "LogBinding":
    return LogBinding(get_logger(), fields)


class LogBinding:
    """携带 job_id/source 等上下文的轻量绑定。"""

    def __init__(self, logger: logging.Logger, fields: dict[str, Any]):
        self._logger = logger
        self._fields = fields

    def bind(self, **fields: Any) -> "LogBinding":
        merged = {**self._fields, **fields}
        return LogBinding(self._logger, merged)

    def _emit(self, level: int, event: str, **fields: Any) -> None:
        merged = {**self._fields, **fields}
        with _lock:
            self._logger.log(level, event, extra=merged)

    def step(self, event: str, step: str, **extra: Any) -> None:
        self._emit(logging.INFO, event, step=step, extra=extra or None)

    def decision(self, event: str, decision: str, basis: Any, **extra: Any) -> None:
        self._emit(logging.INFO, event, decision=decision, basis=basis, extra=extra or None)

    def progress(self, event: str, progress: float, **extra: Any) -> None:
        self._emit(logging.INFO, event, progress=round(progress, 4), extra=extra or None)

    def warning(self, event: str, **extra: Any) -> None:
        self._emit(logging.WARNING, event, extra=extra or None)

    def error(self, event: str, **extra: Any) -> None:
        self._emit(logging.ERROR, event, extra=extra or None)

    def exception(self, event: str, **extra: Any) -> None:
        with _lock:
            self._logger.error(event, exc_info=True, extra={**self._fields, **(extra or {})})
