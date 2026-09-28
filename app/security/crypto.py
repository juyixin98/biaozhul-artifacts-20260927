"""安全内核：静态加密与输入指纹。

* :class:`CryptoBox` —— Fernet (AES-128-CBC + HMAC-SHA256) 对称加解密，
  用于把提交数据落盘为密文；密钥来源可审计（configured / ephemeral）。
* :func:`fingerprint` —— 对规范化输入做 SHA-256 指纹，用于日志关联，
  指纹不可逆，不泄漏数据内容。
* :func:`generate_key` —— 生成合规 Fernet 密钥的辅助/CLI。

注意：这是**传输与静态存储保护**工具，与 k/l 匿名化解决的是不同威胁；
二者都不构成完整隐私保证。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.errors import FailureCode, ServiceError


def generate_key() -> bytes:
    """生成新的 Fernet 密钥（urlsafe base64, 32 字节）。"""
    return Fernet.generate_key()


def normalize_key(raw: str) -> bytes:
    """校验并规范化密钥字符串。"""
    if not raw:
        raise ServiceError(
            FailureCode.INVALID_INPUT,
            "empty encryption key",
        )
    try:
        key = raw.encode("utf-8")
        # Fernet 密钥必须是 32 字节的 urlsafe base64
        decoded = base64.urlsafe_b64decode(key)
        if len(decoded) != 32:
            raise ValueError("key must decode to 32 bytes")
        Fernet(key)  # 触发格式校验
        return key
    except (ValueError, TypeError) as exc:
        raise ServiceError(
            FailureCode.INVALID_INPUT,
            "invalid Fernet key: generate one with `python -m app.security.crypto gen-key`",
        ) from exc


@dataclass
class CryptoBox:
    fernet: Fernet
    key_source: str  # "configured" | "ephemeral"

    def encrypt_json(self, obj: Any) -> bytes:
        data = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return self.fernet.encrypt(data)

    def decrypt_json(self, token: bytes | str) -> Any:
        try:
            raw = self.fernet.decrypt(token if isinstance(token, bytes) else token.encode())
        except InvalidToken as exc:
            raise ServiceError(
                FailureCode.DECRYPTION_FAILED,
                "failed to decrypt stored data: wrong/rotated key or tampered ciphertext",
            ) from exc
        return json.loads(raw.decode("utf-8"))


def build_crypto_box(configured_key: str, allow_ephemeral: bool) -> CryptoBox:
    if configured_key:
        return CryptoBox(Fernet(normalize_key(configured_key)), "configured")
    if allow_ephemeral:
        return CryptoBox(Fernet(generate_key()), "ephemeral")
    raise ServiceError(
        FailureCode.INVALID_INPUT,
        "encryption key not configured and ephemeral keys are disabled; "
        "set ANON_ENCRYPTION_KEY or ANON_ALLOW_EPHEMERAL_KEY=1 for local testing",
    )


def fingerprint(payload: Any) -> str:
    """对任意可 JSON 化输入计算稳定的 SHA-256 指纹（前 16 字节十六进制）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def derive_signing_key(encryption_key: str | bytes, purpose: str = b"audit-chain-v1") -> bytes:
    """从主密钥派生审计 HMAC-SHA256 密钥（域分离，避免密钥复用）。"""
    if isinstance(encryption_key, str):
        encryption_key = encryption_key.encode("utf-8")
    return hmac.new(encryption_key, purpose, hashlib.sha256).digest()
