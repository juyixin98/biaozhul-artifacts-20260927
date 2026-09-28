"""Domain models: votes, signed envelopes, evidence, status enums."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

# Maximum permitted round; canonical encoding is u64 but we reject absurd
# values early so callers cannot slip through negative / huge integers.
MAX_ROUND = 2**32 - 1
MAX_CHAIN_ID_LEN = 64
MAX_VALIDATOR_ID_LEN = 64
MAX_BLOCK_ROOT_LEN = 64


class VoteStatus(str, Enum):
    """Exact classification of one ingest attempt. Never collapsed to a
    generic 'success' — tests assert on these specific categories."""

    ACCEPTED = "accepted"              # first time, valid, no conflict yet
    DUPLICATE_RETRANSMIT = "duplicate_retransmit"  # identical vote seen before
    DOUBLE_VOTE = "double_vote"        # same-target conflicting vote
    SURROUND_VOTE = "surround_vote"    # nested/overlapping interval conflict
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_CHAIN = "invalid_chain"
    INVALID_ROUNDS = "invalid_rounds"
    INVALID_MEMBERSHIP = "invalid_membership"
    UNKNOWN_VALIDATOR = "unknown_validator"
    MALFORMED = "malformed"


# Statuses that constitute a slashable offense (evidence emitted).
SLASHABLE_STATUSES = frozenset({VoteStatus.DOUBLE_VOTE, VoteStatus.SURROUND_VOTE})

# Statuses that count as cryptographic / submission invalidity (counted
# separately from genuine conflicts, per the test requirement).
INVALID_STATUSES = frozenset(
    {
        VoteStatus.INVALID_SIGNATURE,
        VoteStatus.INVALID_CHAIN,
        VoteStatus.INVALID_ROUNDS,
        VoteStatus.INVALID_MEMBERSHIP,
        VoteStatus.UNKNOWN_VALIDATOR,
        VoteStatus.MALFORMED,
    }
)


class ViolationKind(str, Enum):
    DOUBLE_VOTE = "double_vote"
    SURROUND_VOTE = "surround_vote"


@dataclass(frozen=True)
class Vote:
    """Unsigned vote fields (the justification target/source pair)."""

    chain_id: str
    validator_id: str
    source_round: int
    target_round: int
    block_root: bytes  # digest being justified at target_round

    def to_json_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "validator_id": self.validator_id,
            "source_round": self.source_round,
            "target_round": self.target_round,
            "block_root": self.block_root.hex(),
        }

    @staticmethod
    def from_json_dict(d: dict) -> "Vote":
        try:
            block_root = d["block_root"]
            if isinstance(block_root, str):
                block_root = bytes.fromhex(block_root)
            return Vote(
                chain_id=str(d["chain_id"]),
                validator_id=str(d["validator_id"]),
                source_round=int(d["source_round"]),
                target_round=int(d["target_round"]),
                block_root=bytes(block_root),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed vote: {exc}") from exc


@dataclass(frozen=True)
class SignedVote:
    """Vote plus signer pubkey + Ed25519 signature over the canonical payload."""

    vote: Vote
    signer_pubkey: bytes
    signature: bytes

    def to_json_dict(self) -> dict:
        d = self.vote.to_json_dict()
        d["signer_pubkey"] = self.signer_pubkey_hex
        d["signature"] = self.signature.hex()
        return d

    @property
    def signer_pubkey_hex(self) -> str:
        return self.signer_pubkey.hex()

    @staticmethod
    def from_json_dict(d: dict) -> "SignedVote":
        vote = Vote.from_json_dict(d)
        try:
            return SignedVote(
                vote=vote,
                signer_pubkey=bytes.fromhex(str(d["signer_pubkey"])),
                signature=bytes.fromhex(str(d["signature"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed signed vote: {exc}") from exc


@dataclass(frozen=True)
class Evidence:
    """Self-contained, independently re-checkable slashing evidence."""

    evidence_id: str
    kind: ViolationKind
    chain_id: str
    validator_id: str
    weight_epoch: int       # epoch snapshot the slashing weight is taken from
    weight: int             # validator weight at that epoch
    vote_a: SignedVote
    vote_b: SignedVote

    def to_json_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "chain_id": self.chain_id,
            "validator_id": self.validator_id,
            "weight_epoch": self.weight_epoch,
            "weight": self.weight,
            "vote_a": self.vote_a.to_json_dict(),
            "vote_b": self.vote_b.to_json_dict(),
        }

    @staticmethod
    def from_json_dict(d: dict) -> "Evidence":
        return Evidence(
            evidence_id=str(d["evidence_id"]),
            kind=ViolationKind(str(d["kind"])),
            chain_id=str(d["chain_id"]),
            validator_id=str(d["validator_id"]),
            weight_epoch=int(d["weight_epoch"]),
            weight=int(d["weight"]),
            vote_a=SignedVote.from_json_dict(d["vote_a"]),
            vote_b=SignedVote.from_json_dict(d["vote_b"]),
        )


def dumps_canonical_json(obj) -> str:
    """Stable JSON used for storage/transport of evidence (the hash id itself
    is computed over the binary canonical encoding, not over this JSON)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
