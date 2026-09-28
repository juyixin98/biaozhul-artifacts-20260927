"""Local Ed25519 signatures for tamper evidence.

Threat model: keys live on the local operator's machine (generated locally; no
production accounts anywhere in this project).  A signature makes post-hoc
modification of a stored run or audit record detectable: verification recomputes
over the canonical payload and checks the public key registered for the
workspace.  This does not protect against an attacker who also replaces the key
file -- key replacement itself is visible because the stored public key
fingerprint changes.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .evidence import canonical_bytes, sha256_hex


class SignatureError(ValueError):
    pass


def generate_keypair() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def save_private_key(key: Ed25519PrivateKey, path: str | os.PathLike[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    os.chmod(p, 0o600)


def load_or_create_key(path: str | os.PathLike[str]) -> Ed25519PrivateKey:
    p = Path(path)
    if p.exists():
        key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SignatureError(f"{p}: expected Ed25519 private key")
        return key
    key = generate_keypair()
    save_private_key(key, p)
    return key


def public_pem(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def public_fingerprint(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    return sha256_hex(public_pem(key))


def encode_signature(sig: bytes) -> str:
    return base64.b64encode(sig).decode("ascii")


def decode_signature(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _as_bytes(obj: object) -> bytes:
    return obj if isinstance(obj, bytes) else canonical_bytes(obj)


def sign_object(key: Ed25519PrivateKey, obj: object) -> str:
    return encode_signature(key.sign(_as_bytes(obj)))


def verify_object(pub: Ed25519PublicKey, obj: object, signature_b64: str) -> bool:
    try:
        pub.verify(decode_signature(signature_b64), _as_bytes(obj))
        return True
    except (InvalidSignature, ValueError):
        return False


def load_public_pem(pem: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError("expected Ed25519 public key")
    return key
