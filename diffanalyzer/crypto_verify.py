"""Ed25519 签名核验与规范 JSON。

信任模型（刻意简单，不含用户/角色后台）：
- 系统只持有受信任的提交者公钥（配置指向本地 PEM）。
- 策略/证据提交时携带 submitter 标识 + 签名；核验三要素：
  1) 签名存在；2) 公钥已注册；3) 签名对规范字节有效。
- API 进程永不持有私钥；私钥只供本地演示/夹具生成使用。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .models import (
    CryptoError,
    MissingSignatureError,
    UnregisteredKeyError,
)
from .parser import canonical_json_bytes


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:
        raise CryptoError("签名不是合法 base64") from exc


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_pem(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def private_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_public_key(pem: bytes | str) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(
            pem.encode() if isinstance(pem, str) else pem
        )
    except Exception as exc:
        raise CryptoError("无法解析公钥 PEM") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise CryptoError("仅接受 Ed25519 公钥")
    return key


def load_private_key(pem: bytes | str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(
            pem.encode() if isinstance(pem, str) else pem, password=None
        )
    except Exception as exc:
        raise CryptoError("无法解析私钥 PEM") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise CryptoError("仅接受 Ed25519 私钥")
    return key


def sign(private_key: Ed25519PrivateKey, payload: Any) -> str:
    return b64e(private_key.sign(canonical_json_bytes(payload)))


class KeyRegistry:
    """submitter 标识 -> 公钥 的静态信任注册表（来自本地 PEM，无后台）。"""

    def __init__(self, keys: dict[str, Ed25519PublicKey]):
        self._keys = dict(keys)

    @classmethod
    def from_pem_dir(cls, mapping: dict[str, Path]) -> "KeyRegistry":
        keys: dict[str, Ed25519PublicKey] = {}
        for submitter, path in mapping.items():
            keys[submitter] = load_public_key(Path(path).read_bytes())
        return cls(keys)

    @classmethod
    def from_pems(cls, mapping: dict[str, str]) -> "KeyRegistry":
        return cls({k: load_public_key(v) for k, v in mapping.items()})

    def submitters(self) -> list[str]:
        return sorted(self._keys)

    def verify(self, submitter: str, payload: Any, signature_b64: str | None) -> None:
        if signature_b64 is None:
            raise MissingSignatureError(
                "提交缺少 signature 字段", {"submitter": submitter}
            )
        key = self._keys.get(submitter)
        if key is None:
            raise UnregisteredKeyError(
                f"未注册的提交者公钥: {submitter!r}",
                {"submitter": submitter, "known": self.submitters()},
            )
        raw_sig = b64d(signature_b64)
        try:
            key.verify(raw_sig, canonical_json_bytes(payload))
        except InvalidSignature:
            raise CryptoError(
                "签名核验失败：载荷被改动或签名不匹配",
                {"submitter": submitter},
            ) from None
