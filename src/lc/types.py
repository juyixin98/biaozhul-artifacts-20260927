"""Protocol data types for the simplified local test chain.

Wire format (JSON)
------------------
* bytes fields are lowercase hex strings, prefixed ``0x``.
* ``Header``::

      {
        "round":       <non-negative int>,
        "parent_root": "0x.."(32B),
        "body_root":   "0x.."(32B),
        "timestamp":   <unix milliseconds, non-negative int>,
        "next_committee_commitment": null | "0x.."(32B),
      }

* ``CommitteeMember``:: {"public_key": "0x.."(32B Ed25519), "weight": <positive int>}
* ``Committee``::       {"members": [CommitteeMember, ...]}   # canonical: key-sorted
* ``Certificate``::

      {"header_root": "0x.."(32B), "signatures":
        [{"public_key": "0x.."(32B), "signature": "0x.."(64B)}, ...]}

* ``Checkpoint``::      {"header": Header, "committee": Committee}

These types are *parsing-only*: they validate shape and canonical invariants
(e.g. 32-byte hashes, 64-byte signatures, key-sorted committee, unique keys).
They never verify authorization; the chain kernel does that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import Code, LightClientError

HASH32_SIZE = 32
PUBKEY_SIZE = 32
SIGNATURE_SIZE = 64

GENESIS_PARENT = b"\x00" * 32


def _as_bytes(name: str, value: Any, size: int) -> bytes:
    if not isinstance(value, str):
        raise LightClientError(
            Code.MALFORMED_HEADER,
            f"field {name!r} must be a 0x-hex string, got {type(value).__name__}",
            details={"field": name},
        )
    if not value.startswith("0x"):
        raise LightClientError(
            Code.MALFORMED_HEADER,
            f"field {name!r} must be 0x-prefixed hex",
            details={"field": name},
        )
    hex_part = value[2:]
    # Exact-width check BEFORE decoding: bytes.fromhex silently drops a leading
    # zero nibble ("0x006c.." -> 31 bytes of a 32-byte hash), and odd-length
    # hex parses at all. Boundary inputs like 0x00.. roots must not truncate.
    if len(hex_part) != 2 * size:
        raise LightClientError(
            Code.MALFORMED_HEADER,
            f"field {name!r} must encode exactly {size} bytes ({2 * size} hex "
            f"digits), got {len(hex_part)} digits",
            details={
                "field": name,
                "expected_digits": 2 * size,
                "got_digits": len(hex_part),
            },
        )
    try:
        raw = bytes.fromhex(hex_part)
    except ValueError as exc:
        raise LightClientError(
            Code.MALFORMED_HEADER,
            f"field {name!r} is not valid hex: {exc}",
            details={"field": name},
        )
    if len(raw) != size:
        raise LightClientError(
            Code.MALFORMED_HEADER,
            f"field {name!r} must be {size} bytes, got {len(raw)}",
            details={"field": name, "length": len(raw)},
        )
    return raw


def _as_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LightClientError(
            Code.MALFORMED_HEADER,
            f"field {name!r} must be an integer",
            details={"field": name},
        )
    return value


@dataclass(frozen=True)
class Header:
    round: int
    parent_root: bytes
    body_root: bytes
    timestamp_ms: int
    next_committee_commitment: Optional[bytes]

    @staticmethod
    def from_dict(obj: Any) -> "Header":
        if not isinstance(obj, dict):
            raise LightClientError(
                Code.MALFORMED_HEADER, "header must be a JSON object"
            )
        try:
            round_index = _as_int("round", obj.get("round"))
            timestamp_ms = _as_int("timestamp", obj.get("timestamp"))
        except LightClientError:
            raise
        if round_index < 0:
            raise LightClientError(
                Code.MALFORMED_HEADER, "round must be non-negative",
                details={"round": round_index},
            )
        if timestamp_ms < 0:
            raise LightClientError(
                Code.MALFORMED_HEADER, "timestamp must be non-negative",
                details={"timestamp": timestamp_ms},
            )
        parent_root = _as_bytes("parent_root", obj.get("parent_root"), HASH32_SIZE)
        body_root = _as_bytes("body_root", obj.get("body_root"), HASH32_SIZE)
        ncc = obj.get("next_committee_commitment")
        if ncc is not None:
            ncc = _as_bytes(
                "next_committee_commitment", ncc, HASH32_SIZE
            )
        return Header(round_index, parent_root, body_root, timestamp_ms, ncc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round": self.round,
            "parent_root": "0x" + self.parent_root.hex(),
            "body_root": "0x" + self.body_root.hex(),
            "timestamp": self.timestamp_ms,
            "next_committee_commitment": (
                None
                if self.next_committee_commitment is None
                else "0x" + self.next_committee_commitment.hex()
            ),
        }


@dataclass(frozen=True)
class CommitteeMember:
    public_key: bytes
    weight: int


@dataclass(frozen=True)
class Committee:
    members: List[CommitteeMember] = field(default_factory=list)

    @property
    def total_weight(self) -> int:
        return sum(m.weight for m in self.members)

    @staticmethod
    def from_dict(obj: Any, *, max_size: int) -> "Committee":
        if not isinstance(obj, dict) or not isinstance(obj.get("members"), list):
            raise LightClientError(
                Code.MALFORMED_COMMITTEE,
                "committee must be an object with a 'members' list",
            )
        raw_members = obj["members"]
        if not raw_members:
            raise LightClientError(
                Code.MALFORMED_COMMITTEE, "committee must be non-empty"
            )
        if len(raw_members) > max_size:
            raise LightClientError(
                Code.COMMITTEE_TOO_LARGE,
                f"committee size {len(raw_members)} exceeds limit {max_size}",
                details={"size": len(raw_members), "limit": max_size},
            )
        members: List[CommitteeMember] = []
        seen: set = set()
        for i, m in enumerate(raw_members):
            if not isinstance(m, dict):
                raise LightClientError(
                    Code.MALFORMED_COMMITTEE,
                    f"members[{i}] must be an object",
                )
            key = _as_bytes(
                f"members[{i}].public_key", m.get("public_key"), PUBKEY_SIZE
            )
            weight = _as_int(f"members[{i}].weight", m.get("weight"))
            if weight <= 0:
                raise LightClientError(
                    Code.MALFORMED_COMMITTEE,
                    f"members[{i}].weight must be positive, got {weight}",
                    details={"index": i, "weight": weight},
                )
            if key in seen:
                raise LightClientError(
                    Code.MALFORMED_COMMITTEE,
                    f"duplicate public key in committee at index {i}",
                    details={"index": i},
                )
            seen.add(key)
            members.append(CommitteeMember(key, weight))
        # canonical ordering: ascending public key bytes.
        members.sort(key=lambda mm: mm.public_key)
        return Committee(members)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "members": [
                {"public_key": "0x" + m.public_key.hex(), "weight": m.weight}
                for m in self.members
            ]
        }


@dataclass(frozen=True)
class SignatureEntry:
    public_key: bytes
    signature: bytes


@dataclass(frozen=True)
class Certificate:
    header_root: bytes
    signatures: List[SignatureEntry]

    @staticmethod
    def from_dict(obj: Any) -> "Certificate":
        if not isinstance(obj, dict):
            raise LightClientError(
                Code.MALFORMED_CERTIFICATE, "certificate must be a JSON object"
            )
        header_root = _as_bytes(
            "header_root", obj.get("header_root"), HASH32_SIZE
        )
        sigs = obj.get("signatures")
        if not isinstance(sigs, list):
            raise LightClientError(
                Code.MALFORMED_CERTIFICATE,
                "certificate.signatures must be a list",
            )
        entries: List[SignatureEntry] = []
        seen: set = set()
        for i, s in enumerate(sigs):
            if not isinstance(s, dict):
                raise LightClientError(
                    Code.MALFORMED_CERTIFICATE,
                    f"signatures[{i}] must be an object",
                )
            key = _as_bytes(
                f"signatures[{i}].public_key", s.get("public_key"), PUBKEY_SIZE
            )
            if key in seen:
                raise LightClientError(
                    Code.MALFORMED_CERTIFICATE,
                    f"duplicate signer in certificate at index {i}",
                    details={"index": i},
                )
            seen.add(key)
            sig = _as_bytes(
                f"signatures[{i}].signature", s.get("signature"), SIGNATURE_SIZE
            )
            entries.append(SignatureEntry(key, sig))
        return Certificate(header_root, entries)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "header_root": "0x" + self.header_root.hex(),
            "signatures": [
                {
                    "public_key": "0x" + e.public_key.hex(),
                    "signature": "0x" + e.signature.hex(),
                }
                for e in self.signatures
            ],
        }


@dataclass(frozen=True)
class Checkpoint:
    header: Header
    committee: Committee

    @staticmethod
    def from_dict(obj: Any, *, committee_max_size: int) -> "Checkpoint":
        if not isinstance(obj, dict):
            raise LightClientError(
                Code.MALFORMED_CHECKPOINT, "checkpoint must be a JSON object"
            )
        header = Header.from_dict(obj.get("header"))
        committee = Committee.from_dict(
            obj.get("committee"), max_size=committee_max_size
        )
        return Checkpoint(header, committee)

    def to_dict(self) -> Dict[str, Any]:
        return {"header": self.header.to_dict(), "committee": self.committee.to_dict()}
