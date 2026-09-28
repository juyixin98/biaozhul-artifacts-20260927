"""结构化诊断：关联标识、脱敏、JSON 行日志。

- 每个 HTTP 请求有 http_request_id（X-Request-ID，缺省时生成），写入响应头与日志。
- 业务提交另有 request_id（幂等键），日志中两者同时出现。
- 接受/拒绝都记录关键状态（基线、当前快照、并发提交、重叠分区、裁决码）。
- 敏感字段（见配置 redact_fields）在写日志前递归脱敏，只打掩码。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from typing import Any

REDACTED = "***REDACTED***"

_http_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "http_request_id", default="-"
)


def new_http_request_id() -> str:
    return uuid.uuid4().hex


def set_http_request_id(value: str | None) -> str:
    rid = (value or "").strip() or new_http_request_id()
    _http_request_id.set(rid)
    return rid


def get_http_request_id() -> str:
    return _http_request_id.get()


def redact(value: Any, sensitive: frozenset[str] | set[str]) -> Any:
    """递归遍历 dict/list，键名命中敏感集合时值替换为掩码。"""
    if isinstance(value, dict):
        return {
            k: (REDACTED if k.lower() in sensitive and v is not None else redact(v, sensitive))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, sensitive) for v in value]
    return value


class DiagnosticLogger:
    """输出 JSON 行日志；所有字段先脱敏。"""

    def __init__(self, name: str = "lake_txn", redact_fields: frozenset[str] = frozenset()):
        self._logger = logging.getLogger(name)
        self._sensitive = set(redact_fields)
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
        self._logger.propagate = True

    def event(self, level: int, event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "http_request_id": get_http_request_id(),
        }
        payload.update(redact(fields, self._sensitive))
        self._logger.log(level, json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True))

    def info(self, event: str, **fields: Any) -> None:
        self.event(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.event(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.event(logging.ERROR, event, **fields)
