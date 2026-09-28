"""Canonical protocol encoding.

Everything that is signed or hashed goes through the explicit, self-describing
length-prefixed (TLV) encoding defined here. This removes ambiguity (JSON key
ordering, number padding, …) so that:

* a signature binds *exactly* the fields [domain, chain_id, validator,
  source_round, target_round, block_root];
* evidence bundles hash identically on the producer and on an independent
  re-checker.

Wire primitives (all integers big-endian):

    MAGIC    b"LFV1"
    u8       1 fixed byte
    u64      8 bytes
    bytes    u64(length) || raw
    string   u64(length) || utf-8
    list[T]  u64(count) || concat(encode(T))

Tagged containers prefix each field with a one-byte tag, so two different
shapes can never produce the same byte string.
"""
from __future__ import annotations

import hashlib
import struct
from typing import Iterable

from .config import ENCODING_MAGIC

_U64 = struct.Struct(">Q")

# Field tags (stable wire identifiers — never reuse a retired number).
TAG_MAGIC = 0x01
TAG_DOMAIN = 0x10
TAG_CHAIN_ID = 0x11
TAG_VALIDATOR_ID = 0x12
TAG_SOURCE_ROUND = 0x13
TAG_TARGET_ROUND = 0x14
TAG_BLOCK_ROOT = 0x15

# Evidence bundle tags.
TAG_EVIDENCE_KIND = 0x20
TAG_EVIDENCE_CHAIN_ID = 0x21
TAG_EVIDENCE_VALIDATOR = 0x22
TAG_EVIDENCE_VOTE_A = 0x23
TAG_EVIDENCE_VOTE_B = 0x24
TAG_EVIDENCE_WEIGHT_EPOCH = 0x25
TAG_EVIDENCE_WEIGHT = 0x26

# Snapshot tags (checker recomputes weights from the registry independently).
TAG_PUBKEY = 0x30
TAG_EPOCH_INDEX = 0x31
TAG_WEIGHT = 0x32
TAG_SIGNATURE = 0x33


class EncodingError(ValueError):
    """Raised when input cannot be canonically encoded."""


class DecodingError(ValueError):
    """Raised when a canonical byte string is malformed."""


def _tagged_bytes(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _U64.pack(len(value)) + value


def _tagged_u64(tag: int, value: int) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool):
        raise EncodingError(f"tag {tag:#x}: expected int, got {type(value)!r}")
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise EncodingError(f"tag {tag:#x}: u64 out of range: {value}")
    return bytes([tag]) + _U64.pack(value)


def _tagged_string(tag: int, value: str) -> bytes:
    if not isinstance(value, str):
        raise EncodingError(f"tag {tag:#x}: expected str")
    return _tagged_bytes(tag, value.encode("utf-8"))


def encode_u64(value: int) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise EncodingError(f"u64 out of range: {value!r}")
    return _U64.pack(value)


def encode_bytes(value: bytes) -> bytes:
    if not isinstance(value, (bytes, bytearray)):
        raise EncodingError(f"expected bytes, got {type(value)!r}")
    return _U64.pack(len(value)) + bytes(value)


def encode_list(items: Iterable[bytes]) -> bytes:
    items = list(items)
    return _U64.pack(len(items)) + b"".join(_U64.pack(len(i)) + i for i in items)


# --------------------------------------------------------------------------- #
# Signed vote payload
# --------------------------------------------------------------------------- #

def encode_signed_vote_payload(
    *,
    domain: bytes,
    chain_id: str,
    validator_id: str,
    source_round: int,
    target_round: int,
    block_root: bytes,
) -> bytes:
    """Canonical bytes that the validator signs for one vote.

    Binding order (as required): chain domain, validator, source/target
    rounds, and the block-root digest.
    """
    if not isinstance(domain, (bytes, bytearray)) or len(domain) == 0:
        raise EncodingError("domain must be non-empty bytes")
    if not chain_id:
        raise EncodingError("chain_id must be non-empty")
    if not validator_id:
        raise EncodingError("validator_id must be non-empty")
    if not isinstance(block_root, (bytes, bytearray)) or len(block_root) == 0:
        raise EncodingError("block_root must be non-empty bytes")
    if len(block_root) > 1024:
        raise EncodingError("block_root implausibly large (>1024 bytes)")

    return b"".join(
        [
            _tagged_bytes(TAG_MAGIC, ENCODING_MAGIC),
            _tagged_bytes(TAG_DOMAIN, bytes(domain)),
            _tagged_string(TAG_CHAIN_ID, chain_id),
            _tagged_string(TAG_VALIDATOR_ID, validator_id),
            _tagged_u64(TAG_SOURCE_ROUND, source_round),
            _tagged_u64(TAG_TARGET_ROUND, target_round),
            _tagged_bytes(TAG_BLOCK_ROOT, bytes(block_root)),
        ]
    )


def vote_digest(payload: bytes) -> bytes:
    """sha256 of the canonical signed payload — the digest the signature is
    over and the identity used for de-duplication."""
    return hashlib.sha256(payload).digest()


# --------------------------------------------------------------------------- #
# Evidence bundle
# --------------------------------------------------------------------------- #

def _canonical_signed_vote_fields(
    *,
    chain_id: str,
    validator_id: str,
    source_round: int,
    target_round: int,
    block_root: bytes,
    signature: bytes,
    signer_pubkey: bytes,
) -> bytes:
    return b"".join(
        [
            _tagged_string(TAG_CHAIN_ID, chain_id),
            _tagged_string(TAG_VALIDATOR_ID, validator_id),
            _tagged_u64(TAG_SOURCE_ROUND, source_round),
            _tagged_u64(TAG_TARGET_ROUND, target_round),
            _tagged_bytes(TAG_BLOCK_ROOT, block_root),
            _tagged_bytes(TAG_PUBKEY, signer_pubkey),
            _tagged_bytes(TAG_SIGNATURE, signature),
        ]
    )


def canonical_evidence_bundle(
    *,
    kind: str,
    chain_id: str,
    validator_id: str,
    weight_epoch: int,
    weight: int,
    vote_a: dict,
    vote_b: dict,
) -> bytes:
    """Deterministic byte form of an evidence bundle, hashed to get its id.

    ``vote_a`` / ``vote_b`` are the full *signed* vote JSON dicts including
    signature + the validator pubkey; the bundle is therefore self-contained
    and independently re-checkable.
    """
    return b"".join(
        [
            _tagged_bytes(TAG_MAGIC, ENCODING_MAGIC),
            _tagged_string(TAG_EVIDENCE_KIND, kind),
            _tagged_string(TAG_EVIDENCE_CHAIN_ID, chain_id),
            _tagged_string(TAG_EVIDENCE_VALIDATOR, validator_id),
            _tagged_u64(TAG_EVIDENCE_WEIGHT_EPOCH, weight_epoch),
            _tagged_u64(TAG_EVIDENCE_WEIGHT, weight),
            _tagged_bytes(
                TAG_EVIDENCE_VOTE_A,
                _canonical_signed_vote_fields(
                    chain_id=vote_a["chain_id"],
                    validator_id=vote_a["validator_id"],
                    source_round=vote_a["source_round"],
                    target_round=vote_a["target_round"],
                    block_root=bytes.fromhex(vote_a["block_root"]),
                    signature=bytes.fromhex(vote_a["signature"]),
                    signer_pubkey=bytes.fromhex(vote_a["signer_pubkey"]),
                ),
            ),
            _tagged_bytes(
                TAG_EVIDENCE_VOTE_B,
                _canonical_signed_vote_fields(
                    chain_id=vote_b["chain_id"],
                    validator_id=vote_b["validator_id"],
                    source_round=vote_b["source_round"],
                    target_round=vote_b["target_round"],
                    block_root=bytes.fromhex(vote_b["block_root"]),
                    signature=bytes.fromhex(vote_b["signature"]),
                    signer_pubkey=bytes.fromhex(vote_b["signer_pubkey"]),
                ),
            ),
        ]
    )


def evidence_id(bundle: bytes) -> str:
    return "ev_" + hashlib.sha256(bundle).hexdigest()[:32]
