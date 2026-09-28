"""Canonical byte encoding / key utilities.

Specification version "smt-v1".  These encodings are part of the hash
commitment: changing any byte changes every root.  They are intentionally
binary (length-prefixed), never ambiguous string concatenation.

Node preimage tags (domain separation):
    leaf   : b"\\x00" || KEY_BYTES(32) || lp(value_bytes)
    branch : b"\\x01" || lp(left_hash)  || lp(right_hash)
    empty  : b"\\x02smt-v1-empty" || u16be(depth)

where ``lp`` is a 2-byte big-endian length prefix followed by the bytes.
The distinct first byte guarantees a leaf, branch and empty preimage can
never collide.

A present value of b"" (empty string) is a *real stored value* and has a
different leaf hash from a missing key — missing keys have no leaf at all.
"""
from __future__ import annotations

import json
from typing import Any

KEY_BITS = 256
KEY_BYTES = KEY_BITS // 8
HASH_BYTES = 32

TAG_LEAF = b"\x00"
TAG_BRANCH = b"\x01"
TAG_EMPTY = b"\x02smt-v1-empty"
SPEC_VERSION = "smt-v1"

ZERO_HASH = b"\x00" * HASH_BYTES


def lp(data: bytes) -> bytes:
    """16-bit big-endian length prefix (fields here are always < 64 KiB)."""
    if len(data) > 0xFFFF:
        raise ValueError("field too long for 2-byte length prefix")
    return len(data).to_bytes(2, "big") + data


def encode_leaf(key: bytes, value: bytes) -> bytes:
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
    if not isinstance(value, (bytes, bytearray)):
        raise TypeError("leaf value must be bytes")
    return TAG_LEAF + key + lp(bytes(value))


def encode_branch(left_hash: bytes, right_hash: bytes) -> bytes:
    if len(left_hash) != HASH_BYTES or len(right_hash) != HASH_BYTES:
        raise ValueError("child references must be 32-byte hashes")
    return TAG_BRANCH + lp(left_hash) + lp(right_hash)


def encode_empty_preimage(depth: int) -> bytes:
    """Preimage of the empty-subtree hash at a given *depth*.

    depth is the depth of the subtree root (0 = whole tree, 256 = empty leaf
    slot below a branch at depth 255).
    """
    if not 0 <= depth <= KEY_BITS:
        raise ValueError("depth out of range")
    return TAG_EMPTY + depth.to_bytes(2, "big")


def normalize_key(key: bytes | str) -> bytes:
    """Accept 32 raw bytes or a 64-char hex string; reject everything else."""
    if isinstance(key, str):
        if key.startswith("0x"):
            s = key[2:]
        else:
            s = key
        if len(s) != KEY_BYTES * 2:
            raise ValueError("hex key must encode exactly 32 bytes")
        try:
            return bytes.fromhex(s)
        except ValueError as exc:
            raise ValueError("key is not valid hex") from exc
    if isinstance(key, (bytes, bytearray)):
        if len(key) != KEY_BYTES:
            raise ValueError(f"key must be {KEY_BYTES} bytes")
        return bytes(key)
    raise TypeError("key must be bytes or hex str")


def normalize_value(value: bytes | str | None) -> bytes | None:
    """Values are bytes.  ``None`` means *delete*; b"" is a stored empty value."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raise TypeError("value must be bytes, str or None")


def bit_at(key: bytes, depth: int) -> int:
    """The key bit that decides branching at *depth* (0 = MSB of key)."""
    return (key[depth // 8] >> (7 - (depth % 8))) & 1


# ---------------------------------------------------------------------------
# Canonical JSON (used for the journal payload under HMAC).
# Sort keys, no whitespace, ensure_ascii off but bytes are pre-hex-encoded.
# ---------------------------------------------------------------------------
def canonical_json(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
