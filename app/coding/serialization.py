"""规范化（canonical）序列化。

凡需要被签名或被跨进程稳定复现的 JSON，统一走本模块：
排序键、无多余空白、确保不同实现/语言得到完全一致的字节串。
"""
from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_json(data: bytes | str) -> Any:
    """严格解析；空串/非 JSON 一律抛错，由调用方归入 ENVELOPE_MALFORMED。"""
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)
