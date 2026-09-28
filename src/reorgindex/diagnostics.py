"""结构化诊断日志。

每条记录携带 request_id / 内部事件 id 与关键链状态（高度、哈希、权重），
并对地址、公钥等敏感字段做脱敏：只保留首尾少量字符。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import Any

_SENSITIVE_KEYS = {"address", "sender", "recipient", "from", "to", "pubkey", "public_key", "signature"}


def redact(value: Any, keep: int = 6) -> str:
    """对敏感字符串脱敏：abc...xyz；短串整体掩码。"""

    text = str(value)
    if len(text) <= keep * 2:
        return "***"
    return f"{text[:keep]}...{text[-keep:]}"


def scrub(obj: Any, keep: int = 6) -> Any:
    """递归把敏感键对应的值替换为脱敏字符串。"""

    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            if key in _SENSITIVE_KEYS:
                out[key] = redact(val, keep)
            else:
                out[key] = scrub(val, keep)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub(v, keep) for v in obj]
    return obj


class JsonDiagnostics:
    """行式 JSON 诊断器；事件 id 进程内自增，可附 request_id。"""

    def __init__(self, logger_name: str = "reorgindex", level: str = "INFO", stream=None, redact_keep: int = 6):
        self._log = logging.getLogger(logger_name)
        if not self._log.handlers:
            handler = logging.StreamHandler(stream or sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._log.addHandler(handler)
            self._log.propagate = False
        self._log.setLevel(getattr(logging, level.upper(), logging.INFO))
        self._counter = 0
        self._lock = threading.Lock()
        self.redact_keep = redact_keep

    def _next_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"evt-{self._counter:06d}"

    def emit(self, event: str, request_id: str | None = None, level: int = logging.INFO, **fields: Any) -> str:
        event_id = self._next_id()
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event_id": event_id,
            "request_id": request_id,
            "event": event,
            **scrub(fields, self.redact_keep),
        }
        self._log.log(level, json.dumps(record, ensure_ascii=False, sort_keys=True))
        return event_id
