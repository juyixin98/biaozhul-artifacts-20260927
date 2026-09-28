"""Deterministic canonical encoding and rooted hashing ("SSZ-lite").

This is a deliberately small fixed-width little-endian codec, not SSZ or any
public-chain encoding. Every hash is domain-separated so the same bytes in
different structures can never collide:

    header_root(h)         = SHA256(DOM_HEADER   || encode_header(h))
    committee_commitment(c) = SHA256(DOM_COMMITTEE || encode_committee(c))

A committee certificate signs the 32-byte header root under a second domain::

    signed_message = DOM_CERT_SIGN || header_root     # 8 + 32 = 40 bytes

The encoding is fixed-size per type, which also gives us cheap malformed-input
rejection (wrong length cannot be decoded).
"""

from __future__ import annotations

import hashlib
import struct
from typing import Optional

from .types import (
    Committee,
    GENESIS_PARENT,
    HASH32_SIZE,
    Header,
    PUBKEY_SIZE,
)

DOM_HEADER = b"LC\x01HDR"  # 4 bytes
DOM_COMMITTEE = b"LC\x01COM"  # 4 bytes
# 8-byte prefix for the exact 40-byte message members sign.
DOM_CERT_SIGN = b"LCSIGNH\x01"

_U64 = struct.Struct("<Q")
_U32 = struct.Struct("<I")

# round(8) + parent(32) + body(32) + timestamp(8) + flag(1) + ncc(32)
HEADER_FIXED_SIZE = 8 + 32 + 32 + 8 + 1 + 32


def encode_header(header: Header) -> bytes:
    ncc = header.next_committee_commitment or b"\x00" * HASH32_SIZE
    if len(ncc) != HASH32_SIZE:
        raise ValueError("next_committee_commitment must be 32 bytes")
    return b"".join(
        [
            _U64.pack(header.round),
            header.parent_root,
            header.body_root,
            _U64.pack(header.timestamp_ms),
            b"\x01" if header.next_committee_commitment is not None else b"\x00",
            ncc,
        ]
    )


def decode_header(raw: bytes) -> Header:
    if len(raw) != HEADER_FIXED_SIZE:
        raise ValueError(
            f"encoded header must be {HEADER_FIXED_SIZE} bytes, got {len(raw)}"
        )
    cursor = 0
    (round_index,) = _U64.unpack_from(raw, cursor)
    cursor += 8
    parent_root = raw[cursor : cursor + 32]
    cursor += 32
    body_root = raw[cursor : cursor + 32]
    cursor += 32
    (timestamp_ms,) = _U64.unpack_from(raw, cursor)
    cursor += 8
    flag = raw[cursor]
    cursor += 1
    ncc_field = raw[cursor : cursor + 32]
    if flag not in (0, 1):
        raise ValueError(f"invalid next-committee presence flag {flag}")
    ncc: Optional[bytes] = ncc_field if flag == 1 else None
    return Header(round_index, parent_root, body_root, timestamp_ms, ncc)


def header_root(header: Header) -> bytes:
    return hashlib.sha256(DOM_HEADER + encode_header(header)).digest()


def encode_committee(committee: Committee) -> bytes:
    parts = [_U32.pack(len(committee.members))]
    for m in committee.members:
        if len(m.public_key) != PUBKEY_SIZE:
            raise ValueError("committee public keys must be 32 bytes")
        parts.append(m.public_key)
        parts.append(_U64.pack(m.weight))
    return b"".join(parts)


def decode_committee(raw: bytes) -> Committee:
    # Imported here to keep the parsing/crypto layering one-directional.
    from .types import CommitteeMember

    if len(raw) < 4:
        raise ValueError("encoded committee truncated")
    (count,) = _U32.unpack_from(raw, 0)
    stride = PUBKEY_SIZE + 8
    expected = 4 + count * stride
    if len(raw) != expected:
        raise ValueError(
            f"encoded committee length {len(raw)} != expected {expected} "
            f"for {count} members"
        )
    members = []
    cursor = 4
    for _ in range(count):
        key = raw[cursor : cursor + PUBKEY_SIZE]
        (weight,) = _U64.unpack_from(raw, cursor + PUBKEY_SIZE)
        members.append(CommitteeMember(key, weight))
        cursor += stride
    return Committee(members)


def committee_commitment(committee: Committee) -> bytes:
    return hashlib.sha256(DOM_COMMITTEE + encode_committee(committee)).digest()


def certificate_message(header_root_bytes: bytes) -> bytes:
    """The exact byte string that committee members sign for a header."""
    if len(header_root_bytes) != HASH32_SIZE:
        raise ValueError("header_root must be 32 bytes")
    return DOM_CERT_SIGN + header_root_bytes


def genesis_parent() -> bytes:
    return GENESIS_PARENT
