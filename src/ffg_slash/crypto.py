"""Thin wrapper around the ``cryptography`` library's Ed25519.

Signatures bind the domain-separated preimage produced by :mod:`encoding`.
Keys are synthetic/local only (deterministic derivation supported for the
fixtures), no production accounts are involved.
"""

from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from .encoding import PUBKEY_SIZE, SIGNATURE_SIZE, signing_preimage


class SignatureError(ValueError):
    """Malformed key/signature material (length/type problems)."""


def generate_keypair() -> tuple[bytes, bytes]:
    """Return ``(private_seed, public_key)``; the seed is the 32-byte Ed25519 seed."""
    sk = ed25519.Ed25519PrivateKey.generate()
    seed = sk.private_bytes_raw()
    return seed, sk.public_key().public_bytes_raw()


def keypair_from_seed(seed: bytes) -> tuple[bytes, bytes]:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != PUBKEY_SIZE:
        raise SignatureError(f"seed must be {PUBKEY_SIZE} bytes")
    sk = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(seed))
    return bytes(seed), sk.public_key().public_bytes_raw()


def derive_seed(label: str) -> bytes:
    """Deterministic synthetic seed from a label, e.g. ``demo-val-1``."""
    return hashlib.sha256(b"ffg-slash-synthetic-key:" + label.encode("utf-8")).digest()


def sign_vote(
    private_seed: bytes,
    *,
    chain_id: int,
    validator_pubkey: bytes,
    source_epoch: int,
    source_root: bytes,
    target_epoch: int,
    target_root: bytes,
) -> bytes:
    if len(private_seed) != PUBKEY_SIZE:
        raise SignatureError(f"private seed must be {PUBKEY_SIZE} bytes")
    sk = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(private_seed))
    msg = signing_preimage(
        chain_id=chain_id,
        validator_pubkey=validator_pubkey,
        source_epoch=source_epoch,
        source_root=source_root,
        target_epoch=target_epoch,
        target_root=target_root,
    )
    sig = sk.sign(msg)
    if len(sig) != SIGNATURE_SIZE:  # pragma: no cover - Ed25519 always yields 64B
        raise SignatureError("unexpected signature length")
    return sig


def verify_vote(
    pubkey: bytes,
    signature: bytes,
    *,
    chain_id: int,
    validator_pubkey: bytes,
    source_epoch: int,
    source_root: bytes,
    target_epoch: int,
    target_root: bytes,
) -> bool:
    """Return True iff ``signature`` is valid. Never raises on bad crypto input."""
    if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != PUBKEY_SIZE:
        return False
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != SIGNATURE_SIZE:
        return False
    try:
        vk = ed25519.Ed25519PublicKey.from_public_bytes(bytes(pubkey))
        msg = signing_preimage(
            chain_id=chain_id,
            validator_pubkey=validator_pubkey,
            source_epoch=source_epoch,
            source_root=source_root,
            target_epoch=target_epoch,
            target_root=target_root,
        )
        vk.verify(bytes(signature), msg)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
