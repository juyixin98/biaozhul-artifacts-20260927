"""Vote data model, wire envelope parsing and decision statuses."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass

from .encoding import EncodingError, PUBKEY_SIZE, ROOT_SIZE, SIGNATURE_SIZE


class RejectReason(str, enum.Enum):
    """Precise failure categories; unknown/exceptional input is never 'success'."""

    MALFORMED = "malformed"                 # not parseable into a vote
    BAD_EPOCH_ORDER = "bad_epoch_order"     # source_epoch >= target_epoch
    BAD_CHAIN_ID = "bad_chain_id"
    UNKNOWN_VALIDATOR = "unknown_validator"
    INVALID_SIGNATURE = "invalid_signature"
    VALIDATOR_INACTIVE = "validator_inactive"


class IngestStatus(str, enum.Enum):
    ACCEPTED = "accepted"                   # well-formed, signed, member, first seen
    DUPLICATE = "duplicate"                 # byte-identical valid vote re-send
    REJECTED = "rejected"                   # invalid; carries reject_reason


class Offense(str, enum.Enum):
    DOUBLE_VOTE = "double_vote"             # same target epoch, different content
    SURROUND_VOTE = "surround_vote"         # s1<s2 and t2<t1 between two votes


@dataclass(frozen=True)
class Vote:
    chain_id: int
    validator_pubkey: bytes
    source_epoch: int
    source_root: bytes
    target_epoch: int
    target_root: bytes
    signature: bytes

    def validate_shape(self) -> None:
        """Raise EncodingError if field types/lengths are wrong."""
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise EncodingError("chain_id must be an integer")
        if not (0 <= self.chain_id <= 2**64 - 1):
            raise EncodingError("chain_id out of uint64 range")
        if len(self.validator_pubkey) != PUBKEY_SIZE:
            raise EncodingError("validator_pubkey must be 32 bytes")
        for name, epoch in (("source_epoch", self.source_epoch), ("target_epoch", self.target_epoch)):
            if not isinstance(epoch, int) or isinstance(epoch, bool):
                raise EncodingError(f"{name} must be an integer")
            if not (0 <= epoch <= 2**64 - 1):
                raise EncodingError(f"{name} out of uint64 range")
        if len(self.source_root) != ROOT_SIZE:
            raise EncodingError("source_root must be 32 bytes")
        if len(self.target_root) != ROOT_SIZE:
            raise EncodingError("target_root must be 32 bytes")
        if len(self.signature) != SIGNATURE_SIZE:
            raise EncodingError("signature must be 64 bytes")

    def to_envelope(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "validator_pubkey": self.validator_pubkey.hex(),
            "source_epoch": self.source_epoch,
            "source_root": self.source_root.hex(),
            "target_epoch": self.target_epoch,
            "target_root": self.target_root.hex(),
            "signature": self.signature.hex(),
        }


def _hex_field(obj: dict, key: str, size: int) -> bytes:
    raw = obj.get(key)
    # bool is a subclass of int but never a valid hex string anyway; be strict.
    if not isinstance(raw, str):
        raise EncodingError(f"{key} must be a hex string")
    try:
        data = bytes.fromhex(raw)
    except ValueError as exc:
        raise EncodingError(f"{key} is not valid hex") from exc
    if len(data) != size:
        raise EncodingError(f"{key} must decode to {size} bytes, got {len(data)}")
    return data


def _int_field(obj: dict, key: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise EncodingError(f"{key} must be an integer")
    if not (0 <= value <= 2**64 - 1):
        raise EncodingError(f"{key} out of uint64 range")
    return value


def vote_from_envelope(payload: dict | str | bytes) -> Vote:
    """Parse an external JSON envelope into a :class:`Vote`.

    Raises EncodingError on any structural problem; callers must not treat
    unparseable input as a vote of any kind.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise EncodingError("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise EncodingError("payload must be a JSON object")
    vote = Vote(
        chain_id=_int_field(payload, "chain_id"),
        validator_pubkey=_hex_field(payload, "validator_pubkey", PUBKEY_SIZE),
        source_epoch=_int_field(payload, "source_epoch"),
        source_root=_hex_field(payload, "source_root", ROOT_SIZE),
        target_epoch=_int_field(payload, "target_epoch"),
        target_root=_hex_field(payload, "target_root", ROOT_SIZE),
        signature=_hex_field(payload, "signature", SIGNATURE_SIZE),
    )
    vote.validate_shape()
    return vote
