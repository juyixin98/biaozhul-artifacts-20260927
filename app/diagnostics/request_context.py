"""请求身份（request id）生成与传播。"""
from __future__ import annotations

import contextvars
import uuid
from typing import Optional

# 当前请求身份；日志与轨迹都带上它，使一次失败可被完整关联回放。
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def new_request_id() -> str:
    # req_ 前缀便于在日志中识别；时间戳 + uuid4 保证本地唯一。
    return "req_" + uuid.uuid4().hex[:16]


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> str:
    return _request_id_var.get()


def header_or_new(request_id: Optional[str]) -> str:
    if request_id:
        return request_id.strip()[:64]
    return new_request_id()
