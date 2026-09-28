"""Share envelopes, canonical encoding, and the *independent* integrity check.

Why this module exists
----------------------
Vanilla Shamir secret sharing has **no tamper protection**: a single altered
share typically makes reconstruction return garbage without any error. We
therefore add an integrity layer that is deliberately *separate* from the
finite-field math:

* Each collection gets a random 256-bit HMAC key.
* Every share carries an HMAC-SHA256 over the canonical encoding of its bound
  identity + field parameters + payload.
* Recovery verifies the MAC *before* touching any field arithmetic.

Trust boundary (see README for the full discussion)
---------------------------------------------------
The MAC authenticates shares against the server's collection key. It detects
accidental corruption and tampering by anyone who does **not** hold the key.
It does **not** provide non-repudiation: a shareholder who legitimately holds a
valid share can still submit a *different* value that the server cannot by
itself label as the attacker, and if the server key itself leaks, forged shares
pass the MAC. "Recovery failed" therefore never means "we identified every
malicious party" -- it only means the supplied set is unusable/inconsistent.

Nothing in here logs or exposes ``ys`` or secret material; callers log only
:func:`fingerprint`, a keyed-less SHA-256 digest of the envelope.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import struct
from typing import Iterable

from .field import FieldParams

# Field separators are fixed 0x1F (unit separator) / 0x1E (record separator)
# control bytes that never appear in the token vocabulary we build, so the
# canonical byte string is unambiguous without a heavier serialiser.
_SEP = b"\x1f"


def _enc_int(value: int) -> bytes:
    # Big-endian, fixed unsigned encoding of arbitrary non-negative ints.
    if value < 0:
        raise ValueError("cannot canonically encode negative integer")
    length = max(1, (value.bit_length() + 7) // 8)
    return struct.pack(">B", length) + value.to_bytes(length, "big")


def _enc_bytes(value: bytes) -> bytes:
    return _enc_int(len(value)) + value


def _enc_str(value: str) -> bytes:
    return _enc_bytes(value.encode("utf-8"))


@dataclasses.dataclass(frozen=True)
class ShareEnvelope:
    """A single share bound to its collection, threshold and field.

    ``ys`` holds one field element per secret block (a 31-byte chunk each); all
    shares in a collection have equal-length ``ys``.
    """

    collection_id: str
    threshold: int
    total: int
    x: int
    ys: tuple[int, ...]
    field: FieldParams
    mac: str = ""  # hex HMAC-SHA256, filled by :func:`seal`

    # ---- identity / payload encoding (everything the MAC covers) ---------
    def _authenticated_segments(self) -> list[bytes]:
        # ``ys`` is encoded as count followed by each big-endian element padded
        # to the field byte width, so ordering and leading zeros are canonical.
        width = (self.field.prime_bits + 7) // 8
        ys_blob = _enc_int(len(self.ys)) + b"".join(
            _enc_bytes(y.to_bytes(width, "big")) for y in self.ys
        )
        return [
            b"shamir-share-v1",
            _enc_str(self.collection_id),
            _enc_int(self.threshold),
            _enc_int(self.total),
            _enc_int(self.x),
            ys_blob,
            _enc_str(self.field.version),
            _enc_int(self.field.prime),
            _enc_int(self.field.prime_bits),
            _enc_int(self.field.chunk_bytes),
        ]

    def authenticated_bytes(self) -> bytes:
        return _SEP.join(self._authenticated_segments())

    # ---- transport representation ----------------------------------------
    def to_dict(self) -> dict[str, object]:
        return {
            "collection_id": self.collection_id,
            "threshold": self.threshold,
            "total": self.total,
            "x": self.x,
            "ys": [str(y) for y in self.ys],
            "field": self.field.to_dict(),
            "mac": self.mac,
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> "ShareEnvelope":
        field = FieldParams.from_dict(raw["field"])  # type: ignore[index]
        ys = tuple(int(v) for v in raw["ys"])  # type: ignore[arg-type]
        return ShareEnvelope(
            collection_id=str(raw["collection_id"]),
            threshold=int(raw["threshold"]),  # type: ignore[arg-type]
            total=int(raw["total"]),  # type: ignore[arg-type]
            x=int(raw["x"]),  # type: ignore[arg-type]
            ys=ys,
            field=field,
            mac=str(raw.get("mac", "")),
        )

    def public_view(self) -> dict[str, object]:
        """Non-sensitive metadata describing this share (safe-ish to surface)."""
        return {
            "collection_id": self.collection_id,
            "x": self.x,
            "chunks": len(self.ys),
            "threshold": self.threshold,
            "total": self.total,
            "field_version": self.field.version,
            "fingerprint": fingerprint(self),
        }


def _compute_mac(key: bytes, envelope: ShareEnvelope) -> str:
    return hmac.new(key, envelope.authenticated_bytes(), hashlib.sha256).hexdigest()


def seal(envelope: ShareEnvelope, mac_key: bytes) -> ShareEnvelope:
    """Return a copy of ``envelope`` carrying a valid MAC."""
    tag = _compute_mac(mac_key, envelope)
    return dataclasses.replace(envelope, mac=tag)


def verify_mac(envelope: ShareEnvelope, mac_key: bytes) -> bool:
    """Constant-time verification of the share's independent integrity tag."""
    if not envelope.mac:
        return False
    expected = _compute_mac(mac_key, envelope)
    return hmac.compare_digest(expected, envelope.mac)


def fingerprint(envelope: ShareEnvelope) -> str:
    """Short, secret-free digest identifying a share in logs/audit events.

    The digest includes the MAC but never the raw ``ys``; it is preimage
    resistant, so logs leak neither the secret nor the share value.
    """
    material = envelope.authenticated_bytes() + _SEP + envelope.mac.encode()
    return "sha256:" + hashlib.sha256(material).hexdigest()[:16]


def fingerprints(envelopes: Iterable[ShareEnvelope]) -> list[str]:
    return [fingerprint(e) for e in envelopes]
