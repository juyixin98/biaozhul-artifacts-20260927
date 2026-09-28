"""Hash primitives and signed-record helpers.

We use SHA-256 from a mature crypto library (Python's hashlib, backed by
OpenSSL) for all tree hashing, and HMAC-SHA256 (RFC 2104, constant-time
compare) to authenticate journal records.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
from functools import lru_cache

from .encoding import (
    canonical_json,
    encode_branch,
    encode_empty_preimage,
    encode_leaf,
)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hash_leaf(key: bytes, value: bytes) -> bytes:
    return sha256(encode_leaf(key, value))


def hash_branch(left: bytes, right: bytes) -> bytes:
    return sha256(encode_branch(left, right))


@lru_cache(maxsize=1)
def empty_hashes() -> tuple[bytes, ...]:
    """empty_hashes()[d] = hash committing to an empty subtree rooted at depth d.

    Defined strictly level by level::

        empty[256] = SHA256(TAG_EMPTY || u16be(256))                    # base
        empty[d]   = SHA256(TAG_EMPTY || u16be(d) || empty[d+1] || empty[d+1])

    The level-256 base says "this leaf slot is empty"; level d binds the
    depth and both empty children of the level below.  TAG_EMPTY (first
    byte 0x02) keeps these preimages disjoint from leaf (0x00) and branch
    (0x01) preimages, so a commitment to "empty" can never be reinterpreted
    as a node of another kind.
    """
    table = [b""] * (256 + 1)
    table[256] = sha256(encode_empty_preimage(256))
    for depth in range(255, -1, -1):
        child = table[depth + 1]
        table[depth] = sha256(encode_empty_preimage(depth) + child + child)
    return tuple(table)


def empty_at(depth: int) -> bytes:
    return empty_hashes()[depth]


# ---------------------------------------------------------------------------
# Journal record authentication
# ---------------------------------------------------------------------------
JOURNAL_HMAC_TAG = b"smt-v1-journal"


def _mac_key(key_material: str) -> bytes:
    # Domain-separate this HMAC from any other use of the configured secret.
    return _hmac.new(key_material.encode("utf-8"), b"smt-v1-key-derivation/journal", hashlib.sha256).digest()


def sign_payload(payload: dict, key_material: str) -> str:
    """Return hex HMAC over the canonical JSON encoding of *payload*."""
    mac = _hmac.new(_mac_key(key_material), JOURNAL_HMAC_TAG, hashlib.sha256)
    mac.update(canonical_json(payload))
    return mac.hexdigest()


def verify_payload(payload: dict, signature_hex: str, key_material: str) -> bool:
    expected = sign_payload(payload, key_material)
    try:
        supplied = bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        return False
    return _hmac.compare_digest(supplied, bytes.fromhex(expected))
