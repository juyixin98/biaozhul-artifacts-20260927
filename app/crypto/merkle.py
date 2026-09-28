"""Merkle batch root over the ordered field commitments.

Layout (v1):

    leaf_digest = SHA256(MERKLE_DOMAIN || 0x00 || lp(commitment_bytes))
    node_digest = SHA256(MERKLE_DOMAIN || 0x01 || lp(left) || lp(right))

  * the leaf/node prefixes block second-preimage attacks between leaf and
    internal levels;
  * the ordered list covers every (record, field) cell exactly once, in
    ``(record_index, field_position)`` order, so a verifier can detect
    inserted, dropped or reordered cells;
  * a lone odd node is folded with a *duplicate of itself* (never promoted
    raw), which keeps every level structurally unambiguous;
  * the root of an empty batch is the fixed ``empty`` sentinel.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from ..version import MERKLE_SCHEMA_VERSION
from .hashing import sha256

MERKLE_DOMAIN = MERKLE_SCHEMA_VERSION.encode("ascii")
_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"
_EMPTY_ROOT = sha256(MERKLE_DOMAIN + b"empty")


def _lp(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def leaf_digest(commitment_hex: str) -> bytes:
    commitment = bytes.fromhex(commitment_hex)
    if len(commitment) != 32:
        raise ValueError(f"commitment must be 32 bytes, got {len(commitment)}")
    return sha256(MERKLE_DOMAIN + _LEAF_PREFIX + _lp(commitment))


def node_digest(left: bytes, right: bytes) -> bytes:
    return sha256(MERKLE_DOMAIN + _NODE_PREFIX + _lp(left) + _lp(right))


def empty_root_hex() -> str:
    return _EMPTY_ROOT.hex()


@dataclass(frozen=True)
class ProofStep:
    # ``side`` is the side the *sibling* sits on during verification.
    side: str  # "left" | "right"
    hash_hex: str

    def to_dict(self) -> dict:
        return {"side": self.side, "hash_hex": self.hash_hex}

    @classmethod
    def from_dict(cls, raw: dict) -> "ProofStep":
        side = raw.get("side")
        hx = raw.get("hash_hex")
        if side not in ("left", "right") or not isinstance(hx, str):
            raise ValueError("malformed proof step")
        try:
            if len(bytes.fromhex(hx)) != 32:
                raise ValueError("sibling must be 32 bytes")
        except ValueError as exc:
            raise ValueError("malformed sibling hash") from exc
        return cls(side=side, hash_hex=hx.lower())


def build_merkle_tree(
    commitment_hexes: list[str],
) -> tuple[str, list[list[ProofStep]]]:
    """Return (root_hex, merkle_paths) aligned with the input ordering."""
    if not commitment_hexes:
        return empty_root_hex(), []

    level = [leaf_digest(c) for c in commitment_hexes]
    paths: list[list[ProofStep]] = [[] for _ in level]
    # Original leaf indices represented by each node on the current level.
    groups: list[list[int]] = [[i] for i in range(len(level))]

    while len(level) > 1:
        next_level: list[bytes] = []
        next_groups: list[list[int]] = []
        i = 0
        while i < len(level):
            if i + 1 < len(level):
                left, right = level[i], level[i + 1]
                for idx in groups[i]:
                    paths[idx].append(ProofStep(side="right", hash_hex=right.hex()))
                for idx in groups[i + 1]:
                    paths[idx].append(ProofStep(side="left", hash_hex=left.hex()))
                next_level.append(node_digest(left, right))
                next_groups.append(groups[i] + groups[i + 1])
                i += 2
            else:
                # Odd node folds with an identical copy of itself.
                lone = level[i]
                for idx in groups[i]:
                    paths[idx].append(ProofStep(side="right", hash_hex=lone.hex()))
                next_level.append(node_digest(lone, lone))
                next_groups.append(groups[i])
                i += 1
        level = next_level
        groups = next_groups

    return level[0].hex(), paths


def fold_proof(leaf_commitment_hex: str, path: list[ProofStep]) -> str:
    digest = leaf_digest(leaf_commitment_hex)
    for step in path:
        sibling = bytes.fromhex(step.hash_hex)
        if step.side == "right":
            digest = node_digest(digest, sibling)
        else:
            digest = node_digest(sibling, digest)
    return digest.hex()


def verify_proof(
    leaf_commitment_hex: str,
    path: list[ProofStep],
    expected_root_hex: str,
) -> bool:
    try:
        computed = fold_proof(leaf_commitment_hex, path)
    except (ValueError, TypeError):
        return False
    return computed == expected_root_hex.lower()
