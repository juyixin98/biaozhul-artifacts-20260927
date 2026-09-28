"""Domain-separated hashing over audited primitives.

All hashing in the service goes through :func:`labeled_hash`, backed by
``cryptography.hazmat.primitives.hashes``. The digest algorithm is chosen from
an explicit allow-list; unknown names fail closed.
"""
from __future__ import annotations

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives import constant_time as _ct

from app.config import PROTOCOL_VERSION
from app.core.errors import PolicyViolation

_ALLOWED = {
    "sha256": _hashes.SHA256,
    "sha384": _hashes.SHA384,
    "sha512": _hashes.SHA512,
}

# Distinct label per hash *role*. A byte string hashed under one label can
# never validate under another even if the payloads are identical.
LBL_FIELD = f"{PROTOCOL_VERSION}|field-commitment"
LBL_FIELD_LEAF = f"{PROTOCOL_VERSION}|field-tree-leaf"
LBL_FIELD_NODE = f"{PROTOCOL_VERSION}|field-tree-node"
LBL_FIELD_EMPTY = f"{PROTOCOL_VERSION}|field-tree-empty"
LBL_RECORD_LEAF = f"{PROTOCOL_VERSION}|record-tree-leaf"
LBL_RECORD_NODE = f"{PROTOCOL_VERSION}|record-tree-node"
LBL_RECORD_EMPTY = f"{PROTOCOL_VERSION}|record-tree-empty"


def ensure_allowed(digest_name: str) -> None:
    if digest_name not in _ALLOWED:
        raise PolicyViolation(f"digest algorithm {digest_name!r} is not allowed")


def digest_size(digest_name: str) -> int:
    ensure_allowed(digest_name)
    return _ALLOWED[digest_name]().digest_size


def labeled_hash(label: str, digest_name: str, *parts: bytes) -> bytes:
    """Hash ``parts`` under a fixed ``label`` with unambiguous framing.

    Layout::

        H( u16(len(label)) || label || u16(num_parts)
           || u16(len(part_i)) || part_i ... )

    Fixed-width big-endian length prefixes make the tuple shape part of the
    pre-image, so two different tuples can never share an encoding.
    """
    ensure_allowed(digest_name)
    buf = bytearray()
    tag = label.encode("utf-8")
    buf.extend(len(tag).to_bytes(2, "big"))
    buf.extend(tag)
    buf.extend(len(parts).to_bytes(2, "big"))
    for part in parts:
        if not isinstance(part, (bytes, bytearray)):
            raise TypeError("labeled_hash parts must be bytes")
        buf.extend(len(part).to_bytes(2, "big"))
        buf.extend(part)
    digest = _hashes.Hash(_ALLOWED[digest_name]())
    digest.update(bytes(buf))
    return digest.finalize()


def bytes_equal(a: bytes, b: bytes) -> bool:
    """Constant-time equality for secret/response comparisons."""
    return _ct.bytes_eq(bytes(a), bytes(b))
