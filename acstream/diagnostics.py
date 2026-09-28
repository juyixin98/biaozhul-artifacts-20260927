"""诊断：记录每个决策为什么被接受、拒绝或无法判定。

每条记录带：
- request_id：请求级标识（中间件生成/透传 X-Request-ID）；
- sid / version_id：相关实体；
- outcome：accept | reject | undetermined；
- code / message：结论与理由；
- key_state：做决定时的关键状态快照（节点号、偏移、指纹前缀、块长度等）。

敏感数据策略：原始模式与文本字节一律不落诊断表；只记录长度与至多 16 字节
hex 预览（且预览可通过 AC_REDACT_PAYLOADS=false 关闭，默认连预览也不记）。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .storage.db import DiagnosticStore, new_id

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(request_id: str | None) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> str | None:
    return _request_id_var.get()


def sanitize_state(state: dict[str, Any], *, allow_preview: bool) -> dict[str, Any]:
    """递归清洗关键状态：bytes 值只保留长度（可选短 hex 预览）。"""
    clean: dict[str, Any] = {}
    for key, value in state.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            entry: dict[str, Any] = {"type": "bytes", "len": len(raw)}
            if allow_preview:
                entry["hex_preview16"] = raw[:16].hex()
            clean[key] = entry
        elif isinstance(value, dict):
            clean[key] = sanitize_state(value, allow_preview=allow_preview)
        elif isinstance(value, (list, tuple)):
            clean[key] = [
                sanitize_state({"v": v}, allow_preview=allow_preview)["v"]
                if isinstance(v, (dict, bytes, bytearray, memoryview))
                else v
                for v in value
            ]
        else:
            clean[key] = value
    return clean


class Diagnostics:
    def __init__(self, store: DiagnosticStore, *, redact_payloads: bool = True) -> None:
        self._store = store
        self._redact = redact_payloads

    def record(
        self,
        *,
        outcome: str,
        code: str,
        message: str,
        key_state: dict[str, Any] | None = None,
        sid: str | None = None,
        version_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        rid = request_id if request_id is not None else get_request_id()
        clean = sanitize_state(key_state or {}, allow_preview=not self._redact)
        self._store.add(
            event_id=new_id("evt"),
            request_id=rid,
            sid=sid,
            version_id=version_id,
            outcome=outcome,
            code=code,
            message=message,
            key_state=clean,
        )
