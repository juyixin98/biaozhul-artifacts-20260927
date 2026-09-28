"""Field-level commitments.

A field commitment binds **everything** needed to identify and interpret the
field, not just its value:

* protocol version + role label (domain separation),
* batch id (the commitment cannot be replayed into another batch context),
* record index and field position (positional identity),
* canonical field path (named identity),
* declared field type,
* presence state: present / null / missing,
* the value payload (present only) and a per-field CSPRNG salt.

Consequently:

* the same value under two different field names -> different commitments;
* the same value at two different positions -> different commitments;
* swapping two fields changes both the positional and path binding;
* null and missing are distinct tagged states; an empty string is a present
  value and cannot collide with null.
"""
from __future__ import annotations

from typing import Any

from app.core.encoding import (
    STATE_MISSING,
    STATE_NULL,
    STATE_PRESENT,
    encode_present,
)
from app.core.errors import IdentityMismatch, ProofMalformed, TypeEncodingError
from app.security.hashing import LBL_FIELD, labeled_hash


def field_commitment(
    *,
    digest_name: str,
    batch_id: str,
    record_index: int,
    position: int,
    path: str,
    field_type: str,
    state: str,
    value: Any = None,
    salt: bytes | None = None,
) -> bytes:
    """Compute the 32/48/64-byte field commitment.

    ``salt`` is required for present values; it is also supported for null
    (a salted null) but always omitted for ``missing``.
    """
    if record_index < 0 or position < 0:
        raise IdentityMismatch("record index and position must be non-negative")
    if not path:
        raise IdentityMismatch("field path must be non-empty")

    parts: list[bytes] = [
        batch_id.encode("utf-8"),
        record_index.to_bytes(8, "big"),
        position.to_bytes(8, "big"),
        path.encode("utf-8"),
        field_type.encode("utf-8"),
        state.encode("ascii"),
    ]

    if state == STATE_PRESENT:
        _tag, payload = encode_present(field_type, value)
        parts.append(payload)
        if salt is None:
            raise IdentityMismatch(
                "present-value commitment requires a salt (unsalted present "
                "commitments are refused at the service boundary)"
            )
        parts.append(bytes(salt))
    elif state == STATE_NULL:
        parts.append(b"")  # explicit empty payload slot
        parts.append(bytes(salt) if salt is not None else b"")
    elif state == STATE_MISSING:
        parts.append(b"")
        parts.append(b"")
    else:  # pragma: no cover - guarded by parsing layer too
        raise ProofMalformed(f"unknown committed state {state!r}")

    return labeled_hash(LBL_FIELD, digest_name, *parts)


def require_state(state: Any) -> str:
    if not isinstance(state, str) or state not in (
        STATE_PRESENT,
        STATE_NULL,
        STATE_MISSING,
    ):
        raise ProofMalformed(
            f"state must be one of present/null/missing, got {state!r}"
        )
    return state


__all__ = [
    "field_commitment",
    "require_state",
    "TypeEncodingError",
]
