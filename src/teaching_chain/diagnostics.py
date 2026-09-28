"""结构化诊断日志。

每条日志都带：时间戳（仅用于本地排障，**不参与任何共识结果**）、
``request_id``、事件名与关键字段。敏感字段（签名 / 公钥 / 原始字节码）
按固定规则脱敏后再落盘，避免完整敏感数据进入日志。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any

# 这些键的值一律只保留长度 + 前后少量字符
_SECRET_KEYS = {"signature", "signer", "pubkey", "public_key", "private_key"}
# 原始字节码只记录长度与前 8 字节
_HEX_KEYS = {"code", "code_hex"}


def redact(key: str, value: Any, max_hex_prefix: int = 8) -> Any:
    if key in _SECRET_KEYS and isinstance(value, str):
        if len(value) <= 10:
            return f"<redacted:{len(value)}chars>"
        return f"{value[:6]}…({len(value)}字符)"
    if key in _HEX_KEYS and isinstance(value, str):
        body = value[2:] if value.startswith("0x") else value
        return f"<hex {len(body)//2}字节, 前缀 {body[:max_hex_prefix]}…>"
    if isinstance(value, dict):
        return {k: redact(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(key, v) for v in value]
    return value


class JsonDiagnostics:
    """写入 stderr 的一行一条 JSON 诊断器。"""

    def __init__(self, name: str = "teaching-chain", level: int = logging.INFO):
        self.logger = logging.getLogger(name)
        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
            self.logger.setLevel(level)
            self.logger.propagate = False

    def emit(
        self,
        event: str,
        request_id: str,
        *,
        decision: str,
        reason: str = "",
        level: int = logging.INFO,
        **fields: Any,
    ) -> None:
        """记录一条诊断。

        ``decision`` 取 ACCEPT / REJECT / UNDETERMINED，说明请求为何被
        接受、拒绝或无法判定。
        """
        payload = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            "request_id": request_id,
            "decision": decision,
            "reason": reason,
        }
        for k, v in fields.items():
            payload[k] = redact(k, v)
        self.logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))


DIAGNOSTICS = JsonDiagnostics()
