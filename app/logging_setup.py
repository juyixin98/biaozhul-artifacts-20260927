"""结构化日志与脱敏。

诊断要求：日志必须携带记录/请求标识与关键状态；有敏感数据时只打印脱敏信息。
因此值永不记录，键只记录不可逆指纹（SHA-256 前 12 位）。
"""
from __future__ import annotations

import hashlib
import logging
import sys

REDACTION = "<redacted>"
KEY_FINGERPRINT_BYTES = 6  # 12 个 hex 字符，足够在合成环境中人工对应


def key_fingerprint(key: bytes | str) -> str:
    """键的脱敏指纹：sha256 前 6 字节。"""
    if isinstance(key, str):
        key = bytes.fromhex(key)
    return hashlib.sha256(key).hexdigest()[: KEY_FINGERPRINT_BYTES * 2]


def redact_keys(keys: list[bytes]) -> list[str]:
    return [key_fingerprint(k) for k in keys]


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("smt_state")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s level=%(levelname)s req=%(request_id)s %(message)s",
                defaults={"request_id": "-"},
            )
        )
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


class RequestAdapter(logging.LoggerAdapter):
    """为每条日志绑定 request_id（不携带任何原始键值数据）。"""

    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra["request_id"] = self.extra.get("request_id", "-")
        return msg, kwargs
