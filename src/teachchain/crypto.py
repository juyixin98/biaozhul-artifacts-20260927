"""编码与验签：Ed25519 签名 + SHA-256 摘要 + 规范地址。

* 账户地址由公钥派生：``0x`` + sha256(Raw 公钥) 前 8 字节（16 hex），
  教学链长度刻意做短；地址与公钥绑定，私钥仅用于本地合成夹具。
* 交易摘要对规范化后的 JSON（sort_keys、无空白）计算，签名再覆盖该摘要，
  保证「执行结果绑定输入摘要」可复核。
* 所有哈希/签名均为确定性算法，不含时间/随机成分。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ADDRESS_BYTES = 8


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: object) -> bytes:
    """确定性 JSON：键排序、无多余空白、ensure_ascii=False。"""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest_payload(obj: object) -> str:
    return sha256_hex(canonical_json(obj))


def address_from_public_key(pub: Ed25519PublicKey) -> str:
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "0x" + sha256_hex(raw)[: ADDRESS_BYTES * 2]


# ---- 密钥工具（仅本地合成夹具使用；服务端绝不生成/持有私钥） ----

def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_to_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_to_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def private_key_from_pem(pem: bytes) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(pem, password=None)


def public_key_from_pem(pem: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem)
    assert isinstance(key, Ed25519PublicKey)
    return key


def private_key_to_raw_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(
        key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode()


@dataclass(frozen=True)
class Signer:
    """本地测试/CLI 用签名器。"""

    key: Ed25519PrivateKey

    @property
    def address(self) -> str:
        return address_from_public_key(self.key.public_key())

    def sign_digest_b64(self, digest_hex: str) -> str:
        return base64.b64encode(self.key.sign(bytes.fromhex(digest_hex))).decode()

    def public_pem(self) -> bytes:
        return public_key_to_pem(self.key.public_key())


def verify_signature_b64(public_pem: bytes, digest_hex: str, signature_b64: str) -> bool:
    """验签，失败返回 False（不抛异常），便于准入层分类拒绝。"""
    try:
        pub = public_key_from_pem(public_pem)
        sig = base64.b64decode(signature_b64, validate=True)
        pub.verify(sig, bytes.fromhex(digest_hex))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
