"""Ed25519 签名与验签（成熟密码库 cryptography）。

对每个版本的检查点（root/version/parent_root/batch_id）签名，
离线回放与 /verify 端点用受信公钥独立验签。
"""
from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .serialization import canonical_json


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_to_hex(key: Ed25519PrivateKey) -> str:
    return key.private_bytes_raw().hex()


def private_key_from_hex(hex_key: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(hex_key))


def private_key_to_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def private_key_from_pem(pem: bytes) -> Ed25519PrivateKey:
    loaded = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ValueError("密钥文件不是 Ed25519 私钥")
    return loaded


def public_key_to_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def public_key_from_pem(pem: bytes) -> Ed25519PublicKey:
    loaded = serialization.load_pem_public_key(pem)
    if not isinstance(loaded, Ed25519PublicKey):
        raise ValueError("公钥文件不是 Ed25519 公钥")
    return loaded


def checkpoint_message(
    version: int, root_hex: str, parent_root_hex: str | None, batch_id: str
) -> bytes:
    """版本检查点的签名载荷。版本号绑定在消息内，防止检查点跨版本重放。"""
    return canonical_json(
        {
            "schema": "smt-checkpoint/v1",
            "version": version,
            "root": root_hex,
            "parent_root": parent_root_hex,
            "batch_id": batch_id,
        }
    )


def sign_checkpoint(
    key: Ed25519PrivateKey,
    version: int,
    root_hex: str,
    parent_root_hex: str | None,
    batch_id: str,
) -> bytes:
    return key.sign(checkpoint_message(version, root_hex, parent_root_hex, batch_id))


def verify_checkpoint(
    public_key: Ed25519PublicKey,
    version: int,
    root_hex: str,
    parent_root_hex: str | None,
    batch_id: str,
    signature: bytes,
) -> bool:
    try:
        public_key.verify(signature, checkpoint_message(version, root_hex, parent_root_hex, batch_id))
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False
