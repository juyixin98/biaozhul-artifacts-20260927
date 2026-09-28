"""结构化日志：JSON 行写入文件并可镜像到控制台。

每条记录都带 ``service`` 版本，并可通过 :class:`log_context` 关联
``run_id`` / ``correlation_id`` / ``event``，满足“日志能关联输入或运行身份、
显示版本、进度与判定依据”的要求。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings

_current: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "anon_risk_log_ctx", default={}
)

_USED_FILES: dict[str, logging.Logger] = {}
_LOCK = threading.Lock()


class log_context:  # noqa: N801 - 作为上下文管理器使用，保持小写可读性
    """在 with 块内为所有日志追加关联字段（run_id / correlation_id 等）。"""

    def __init__(self, **fields: object):
        self.fields = fields
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "log_context":
        merged = {**_current.get(), **self.fields}
        self._token = _current.set(merged)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _current.reset(self._token)


def bind(**fields: object) -> None:
    """永久（当前任务上下文）绑定字段。"""
    _current.set({**_current.get(), **fields})


def current_context() -> dict:
    return dict(_current.get())


class JsonLineFormatter(logging.Formatter):
    def __init__(self, service: str, version: str, metric_version: str):
        super().__init__()
        self.service = service
        self.version = version
        self.metric_version = metric_version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "version": self.version,
            "metric_version": self.metric_version,
            "message": record.getMessage(),
        }
        payload.update(_current.get())
        # 结构化事件字段（进度、判定依据等），以 event_ 前缀或 extra 传入
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            payload["event"] = event
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(settings: Settings) -> logging.Logger:
    """配置根日志器（幂等：重复调用复用同一文件处理器）。"""
    root = logging.getLogger("anon_risk")
    root.setLevel(settings.log_level_int())
    root.handlers.clear()
    root.propagate = False

    log_dir = Path(settings.storage.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "anon-risk.log.jsonl"

    json_fmt = JsonLineFormatter(
        service=settings.app.name,
        version=settings.app.version,
        metric_version=settings.app.metric_version,
    )

    file_handler: logging.Handler
    with _LOCK:
        existing = _USED_FILES.get(str(log_file))
        if existing is not None:
            # 复用文件，但仍挂到当前 logger
            file_handler = next(
                (h for h in existing.handlers if isinstance(h, logging.FileHandler)),
                logging.FileHandler(log_file, encoding="utf-8"),
            )
        else:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            _USED_FILES[str(log_file)] = logging.getLogger(f"anon_risk.file.{log_file}")
            _USED_FILES[str(log_file)].addHandler(file_handler)
    file_handler.setFormatter(json_fmt)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    stream.setLevel(settings.log_level_int())
    root.addHandler(stream)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"anon_risk.{name}")
