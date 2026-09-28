"""Hash primitives: transaction ids, Merkle root and block hashing / PoW.

Block identity hash::

    H = sha256(canonical_json({...unsigned header fields...}))

A block is sealed by appending ``nonce`` and ``pow_signature``:

* ``nonce`` is an ASCII decimal counter; the block passes proof-of-work when
  ``int(H, 16) <= 2**256 // difficulty`` (with H computed over the unsigned
  header including ``nonce``);
* ``pow_signature`` is the block producer's Ed25519 signature over the exact
  same 32-byte H, binding the winning nonce to an authorized producer.
"""
from __future__ import annotations

import hashlib
from typing import Iterable

from .encoding import canonical_json

ZERO_HASH = "0" * 64


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _h(a: str, b: str) -> str:
    return sha256_hex(bytes.fromhex(a) + bytes.fromhex(b))


def merkle_root(txids: list[str]) -> str:
    """Bitcoin-style duplicated-tail Merkle tree over txids (hex)."""
    if not txids:
        return ZERO_HASH
    level = list(txids)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [_h(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


# Header fields covered by the block identity hash (note: nonce IS covered;
# the producer signature is excluded and signs the hash itself).
HEADER_FIELDS = (
    "version",
    "height",
    "parent",
    "merkle_root",
    "difficulty",
    "timestamp",
    "producer",
    "nonce",
)


def unsigned_header(block: dict) -> dict:
    return {field: block[field] for field in HEADER_FIELDS}


def block_identity_hash(block: dict) -> str:
    return sha256_hex(canonical_json(unsigned_header(block)))


def pow_target(difficulty: int) -> int:
    return (1 << 256) // difficulty


def pow_satisfied(block_hash_hex: str, difficulty: int) -> bool:
    return int(block_hash_hex, 16) <= pow_target(difficulty)


def iterate_nonces() -> Iterable[int]:
    n = 0
    while True:
        yield n
        n += 1
