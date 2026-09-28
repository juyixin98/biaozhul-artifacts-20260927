"""密码学组件（全部本地、确定性派生）。

- Fernet (AES-128-CBC + HMAC)：加密证据中的 Authorization/Cookie/响应体；
- HMAC-SHA256 哈希链：审计事件逐条链接，prev_hash/entry_hash 可独立重放核验。

每个 run 使用 HKDF 从主密钥 + run_id 派生子密钥，实现运行间密钥隔离。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from .errors import ComputationFailureError, ErrorCode

GENESIS_HASH = "0" * 64


def normalize_master_key(raw: str | bytes) -> bytes:
    """接受 32 字节原始密钥（base64/hex）或任意长度口令串，归一为 32 字节。"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) == 32:
        return raw
    text = raw
    # 尝试 hex / base64 解出 32 字节
    for decoder in (bytes.fromhex, lambda b: base64.urlsafe_b64decode(b)):
        try:
            got = decoder(text.decode("ascii"))
            if len(got) == 32:
                return got
        except Exception:
            pass
    return hashlib.sha256(raw).digest()


def derive_run_key(master: bytes, run_id: str, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=run_id.encode("utf-8"), info=info
    ).derive(master)


class RunCrypto:
    """单运行的加密器与事件链计算器。"""

    def __init__(self, master_key: str | bytes, run_id: str):
        try:
            master = normalize_master_key(master_key)
            fernet_material = base64.urlsafe_b64encode(
                derive_run_key(master, run_id, b"audit-fernet-v1")
            )
            self._fernet = Fernet(fernet_material)
            self._chain_key = derive_run_key(master, run_id, b"audit-chain-v1")
        except Exception as exc:  # pragma: no cover - normalize 已兜底
            raise ComputationFailureError(
                "主密钥派生失败", code=ErrorCode.KEY_DERIVATION_FAILED,
                details={"error": str(exc)},
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ComputationFailureError(
                "密文无法解开（密钥不匹配或数据损坏）",
                code=ErrorCode.CIPHERTEXT_INVALID,
            ) from exc

    def chain_hash(self, seq: int, event_type: str, timestamp: str,
                   payload: dict[str, Any], prev_hash: str) -> str:
        body = json.dumps(
            {"seq": seq, "event_type": event_type, "timestamp": timestamp,
             "payload": payload, "prev_hash": prev_hash},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(self._chain_key, body, hashlib.sha256).hexdigest()
