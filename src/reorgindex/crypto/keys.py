"""Key material, addresses and Ed25519 signing/verification.

Mature crypto: :mod:`cryptography` (PyCA) Ed25519 only.  Private keys are
generated for the local fixtures; the diagnostics layer never logs them (see
``reorgindex.diag.masking``).

Address = first 20 bytes of SHA-256(raw Ed25519 public key), rendered as
``rx1...`` lower-case hex, mirroring a hash-style address without implying any
real network.
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ADDRESS_PREFIX = "rx1"


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_from_pem(pem: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def private_key_to_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_bytes(key: Ed25519PublicKey | Ed25519PrivateKey) -> bytes:
    if isinstance(key, Ed25519PrivateKey):
        key = key.public_key()
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def public_key_from_hex(hex_value: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_value))


def address_from_pubkey(pubkey_hex: str) -> str:
    digest = hashlib.sha256(bytes.fromhex(pubkey_hex)).digest()[:20]
    return ADDRESS_PREFIX + digest.hex()


def address_from_public_key(key: Ed25519PublicKey) -> str:
    return address_from_pubkey(public_key_bytes(key).hex())


def address_from_private_key(key: Ed25519PrivateKey) -> str:
    return address_from_public_key(key.public_key())


def sign(key: Ed25519PrivateKey, message: bytes) -> bytes:
    return key.sign(message)


def verify(key: Ed25519PublicKey, signature: bytes, message: bytes) -> bool:
    try:
        key.verify(signature, message)
        return True
    except InvalidSignature:
        return False
