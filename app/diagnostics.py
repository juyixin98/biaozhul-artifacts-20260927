"""诊断模块:带请求/记录标识的结构化日志,敏感内容只输出脱敏指纹。

每条日志是一个 JSON 对象,至少包含:
- ts / level / event
- request_id:请求标识(由 API 层生成或取自 X-Request-ID)
- 关键状态字段(哈希前缀、行数、冲突数、决策原因等)

任何文本内容(base/local/remote/result 等)都不会写入日志,
只写 sha256 前 12 位与字符数。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

#: 这些字段名一律视为敏感内容,落日志前替换为指纹。
SENSITIVE_KEYS = frozenset(
    {"base", "local", "remote", "text", "content", "result_text", "resolved_text"}
)


def fingerprint(text: str) -> dict[str, Any]:
    """文本的脱敏指纹:哈希前缀 + 长度,不含内容本身。"""
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        "chars": len(text),
    }


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {
            k: (fingerprint(v) if k in SENSITIVE_KEYS and isinstance(v, str) else _sanitize(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(_sanitize(extra))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def get_logger(log_path: str | None = None, name: str = "merge3") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler: logging.Handler
        if log_path:
            handler = logging.FileHandler(log_path, encoding="utf-8")
        else:
            handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """写一条结构化诊断。fields 中的敏感字段会被自动脱敏。"""
    logger.info(event, extra={"fields": fields})
