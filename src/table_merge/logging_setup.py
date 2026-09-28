"""日志：每个请求带 run_id，输出版本、阶段进度与判定依据。

日志行格式（键值对，方便机器检索）：
    time LEVEL run_id=<rid> event=<event> key=value ...
run_id 关联输入（请求）与一次完整运行，测试日志同样注入它。
"""
from __future__ import annotations

import logging
import sys

from . import __version__

_CONFIGURED = False


class _RunIdFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.run_id = "-"

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = getattr(record, "run_id", None) or self.run_id
        record.version = __version__
        return True


_RUN_FILTER = _RunIdFilter()
_FORMATTER = logging.Formatter(
    fmt="%(asctime)s %(levelname)-5s run_id=%(run_id)s v=%(version)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


def setup_logging(level: str = "INFO", file: str | None = None) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("table_merge")
    logger.setLevel(level)
    logger.propagate = False
    if _CONFIGURED:
        return logger

    handler: logging.Handler
    if file:
        handler = logging.FileHandler(file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_FORMATTER)
    handler.addFilter(_RUN_FILTER)
    logger.addHandler(handler)
    _CONFIGURED = True
    return logger


def set_run_id(run_id: str | None) -> None:
    _RUN_FILTER.run_id = run_id or "-"


def get_logger() -> logging.Logger:
    return setup_logging()
