"""Field-level commitment primitive — the security kernel.

Construction (v1, all integers unsigned big-endian):

    C = SHA256(
          DOMAIN_COMMIT                     fixed domain separator
        || lp(field_name)                   4-byte length + UTF-8 bytes
        || u64(record_index)                record position in the batch
        || u64(field_position)              field position in the schema
        || lp(encoded_value)                4-byte length + canonical typed bytes
                                            (``\\xff\\xff`` = field missing)
        || u64(len(salt)) || salt           random per-(record,field) salt
    )

Identity-binding properties:
  * field name *and* both positions are inside the hash ⇒ a disclosed value
    cannot be moved to another field or another record ("field swapping");
  * the canonical typed encoding is inside the hash ⇒ 1 (int) cannot be
    presented as "1" (string), null vs empty vs missing are distinct;
  * a fresh 128-bit random salt per cell hides even high-entropy values from
    an offline dictionary attack and makes equal values at different cells
    have independent commitments.

Low-entropy caveat (see docs/DESIGN.md §4): an UNSALTED field commitment is
dictionary-enumerable by anyone who sees it. Unsalted commitments are
supported for deterministic cross-batch correlation only and every batch
carries explicit warnings for them. Even salted commitments reveal the value
to the holder of a disclosed salt, so disclosed salts must be treated as
secret material shared only with the intended verifier.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from ..version import COMMITMENT_SCHEMA_VERSION
from .hashing import random_salt, sha256

DOMAIN_COMMIT = COMMITMENT_SCHEMA_VERSION.encode("ascii")


def _u64(n: int) -> bytes:
    if not (0 <= n < 2**64):
        raise ValueError(f"u64 out of range: {n}")
    return struct.pack(">Q", n)


def _lp(data: bytes) -> bytes:
    if not (0 <= len(data) < 2**32):
        raise ValueError("length-prefixed data too large")
    return struct.pack(">I", len(data)) + data


@dataclass(frozen=True)
class CommitmentInput:
    """Exactly the tuple that a field commitment binds to."""

    record_index: int
    field_position: int
    field_name: str
    encoded_value: bytes
    salt: bytes  # b"" means explicitly unsalted

    def salt_hex(self) -> str:
        return self.salt.hex()


@dataclass(frozen=True)
class CommitmentResult:
    commitment_hex: str
    salt_hex: str


def commit_field(inp: CommitmentInput) -> CommitmentResult:
    material = (
        DOMAIN_COMMIT
        + _lp(inp.field_name.encode("utf-8"))
        + _u64(inp.record_index)
        + _u64(inp.field_position)
        + _lp(inp.encoded_value)
        + _u64(len(inp.salt))
        + inp.salt
    )
    return CommitmentResult(commitment_hex=sha256(material).hex(), salt_hex=inp.salt.hex())


def mint_salt(num_bytes: int) -> str:
    return random_salt(num_bytes).hex()
