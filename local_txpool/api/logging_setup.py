"""结构化日志与请求上下文。

每条日志都带 request_id（由中间件从 ``X-Request-Id`` 读取或生成）、
模块名与服务版本，使接口结果可与审计表/日志逐请求关联复核。
"""

from __future__ import annotations

import contextvars
import logging
import sys

from .. import __version__

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def set_request_id(request_id: str) -> contextvars.Token[str]:
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token[str]) -> None:
    _request_id_var.reset(token)


def get_request_id() -> str:
    return _request_id_var.get()


class _RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        record.service_version = __version__
        return True


def configure_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("local_txpool")
    if logger.handlers:
        logger.setLevel(level)
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [v%(service_version)s] "
            "[%(request_id)s] %(name)s: %(message)s"
        )
    )
    handler.addFilter(_RequestContextFilter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
