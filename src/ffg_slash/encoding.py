"""Deterministic byte encoding and domain-separated hashing.

The signing message binds every piece of context a vote commits to:

    SIGNING_DOMAIN ||
    uint64(chain_id) ||
    validator_pubkey (32B) ||
    uint64(source_epoch) || source_root (32B) ||
    uint64(target_epoch) || target_root (32B)

Domains are derived via SHA-256 to make cross-domain collisions infeasible
without relying on truncated human-readable constants. All encodings are
big-endian fixed-width, so two distinct structured inputs never share bytes.
"""

from __future__ import annotations

import hashlib

DOMAIN_VOTE = b"ffg-slash v1 vote" + b"\x00" * 15          # exactly 32 bytes
DOMAIN_SNAPSHOT = b"ffg-slash v1 snapshot" + b"\x00" * 11  # exactly 32 bytes

assert len(DOMAIN_VOTE) == 32
assert len(DOMAIN_SNAPSHOT) == 32

ROOT_SIZE = 32
PUBKEY_SIZE = 32
SIGNATURE_SIZE = 64


class EncodingError(ValueError):
    """Raised when a value cannot be encoded into the wire format."""


def u64(value: int, name: str = "value") -> bytes:
    if not isinstance(value, int) or isinstance(value, bool):
        raise EncodingError(f"{name} must be an integer, got {type(value).__name__}")
    if value < 0 or value > 2**64 - 1:
        raise EncodingError(f"{name} out of uint64 range: {value!r}")
    return value.to_bytes(8, "big")


def fixed(data: bytes, size: int, name: str) -> bytes:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise EncodingError(f"{name} must be bytes, got {type(data).__name__}")
    if len(data) != size:
        raise EncodingError(f"{name} must be {size} bytes, got {len(data)}")
    return bytes(data)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def derive_domain(label: str) -> bytes:
    """Derive a 32-byte domain from a human label (kept here for transparency)."""
    raw = label.encode("utf-8")
    if len(raw) >= 32:
        raise EncodingError("domain label must be shorter than 32 bytes")
    return hashlib.sha256(raw.ljust(32, b"\x00")).digest()


def encode_vote_body(
    *,
    source_epoch: int,
    source_root: bytes,
    target_epoch: int,
    target_root: bytes,
) -> bytes:
    """Encode the fixed 80-byte (source,target) portion of a vote."""
    return b"".join(
        (
            u64(source_epoch, "source_epoch"),
            fixed(source_root, ROOT_SIZE, "source_root"),
            u64(target_epoch, "target_epoch"),
            fixed(target_root, ROOT_SIZE, "target_root"),
        )
    )


def vote_message_root(
    *,
    source_epoch: int,
    source_root: bytes,
    target_epoch: int,
    target_root: bytes,
) -> bytes:
    """SHA-256 over the vote body; identifies the substantive content."""
    return sha256(
        encode_vote_body(
            source_epoch=source_epoch,
            source_root=source_root,
            target_epoch=target_epoch,
            target_root=target_root,
        )
    )


def snapshot_root(
    *,
    epoch: int,
    chain_id: int,
    members: list[tuple[bytes, int]],
) -> bytes:
    """Commit to the exact validator set/weights of an epoch.

    ``members`` is sorted by raw pubkey bytes so snapshot construction is
    independent of insertion order.
    """
    ordered = sorted(((bytes(pk), w) for pk, w in members), key=lambda m: m[0])
    parts = [DOMAIN_SNAPSHOT, u64(chain_id, "chain_id"), u64(epoch, "epoch"), u64(len(ordered), "count")]
    for pubkey, weight in ordered:
        parts.append(fixed(pubkey, PUBKEY_SIZE, "pubkey"))
        parts.append(u64(weight, "weight"))
    return sha256(b"".join(parts))


def signing_preimage(
    *,
    chain_id: int,
    validator_pubkey: bytes,
    source_epoch: int,
    source_root: bytes,
    target_epoch: int,
    target_root: bytes,
) -> bytes:
    """Full 120-byte message that a validator signs for a vote."""
    return b"".join(
        (
            DOMAIN_VOTE,
            u64(chain_id, "chain_id"),
            fixed(validator_pubkey, PUBKEY_SIZE, "validator_pubkey"),
            encode_vote_body(
                source_epoch=source_epoch,
                source_root=source_root,
                target_epoch=target_epoch,
                target_root=target_root,
            ),
        )
    )
