"""Slashing evidence: conflict predicates, evidence packets, canonical IDs.

An evidence packet is self-contained and independently re-checkable: it carries
both votes, the exact target-epoch weight snapshots they were judged against,
and a canonical content id.  Nothing here marks anyone slashed — marking is the
detector's job and happens *only* after cryptographic verification plus rule
matching have succeeded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .encoding import vote_message_root
from .models import Offense, Vote
from .registry import EpochSnapshot


# ---------------------------------------------------------------- predicates

def votes_identical(v1: Vote, v2: Vote) -> bool:
    """Byte-level identity of the substantive vote + signature envelope.

    A re-transmission of the identical vote is a duplicate, never an offense.
    """
    return (
        v1.chain_id == v2.chain_id
        and v1.validator_pubkey == v2.validator_pubkey
        and v1.source_epoch == v2.source_epoch
        and v1.source_root == v2.source_root
        and v1.target_epoch == v2.target_epoch
        and v1.target_root == v2.target_root
        and v1.signature == v2.signature
    )


def same_target_different_content(v1: Vote, v2: Vote) -> bool:
    """Double vote: equal target round, different committed content."""
    if v1.target_epoch != v2.target_epoch:
        return False
    r1 = vote_message_root(
        source_epoch=v1.source_epoch, source_root=v1.source_root,
        target_epoch=v1.target_epoch, target_root=v1.target_root)
    r2 = vote_message_root(
        source_epoch=v2.source_epoch, source_root=v2.source_root,
        target_epoch=v2.target_epoch, target_root=v2.target_root)
    return r1 != r2


def surrounds(v_outer: Vote, v_inner: Vote) -> bool:
    """Strict nesting: outer source < inner source < inner target < outer target.

    Equal endpoints on either side are NOT a surround (explicit definition).
    """
    return (v_outer.source_epoch < v_inner.source_epoch
            and v_inner.target_epoch < v_outer.target_epoch)


def surround_pair(v1: Vote, v2: Vote) -> tuple[Vote, Vote] | None:
    """Return ``(outer, inner)`` if either direction nests, else None."""
    if surrounds(v1, v2):
        return v1, v2
    if surrounds(v2, v1):
        return v2, v1
    return None


def classify_conflict(v1: Vote, v2: Vote) -> Offense | None:
    """Single precise classification for two distinct same-validator votes."""
    if same_target_different_content(v1, v2):
        return Offense.DOUBLE_VOTE
    if surround_pair(v1, v2) is not None:
        return Offense.SURROUND_VOTE
    return None


# ----------------------------------------------------------------- packets

def _snapshot_json(snap: EpochSnapshot) -> dict:
    return {
        "epoch": snap.epoch,
        "chain_id": snap.chain_id,
        "root": snap.root().hex(),
        "members": [
            {"pubkey": pk.hex(), "weight": w} for pk, w in snap.member_list()
        ],
    }


def canonical_core(packet: dict) -> bytes:
    """Canonical serialization of all identity-bearing packet fields."""
    core = {
        "version": packet["version"],
        "type": packet["type"],
        "chain_id": packet["chain_id"],
        "validator_pubkey": packet["validator_pubkey"],
        "vote_1": packet["vote_1"],
        "vote_2": packet["vote_2"],
        "vote_1_snapshot": packet["vote_1_snapshot"],
        "vote_2_snapshot": packet["vote_2_snapshot"],
    }
    return json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def evidence_id_of(packet: dict) -> str:
    return hashlib.sha256(canonical_core(packet)).hexdigest()


@dataclass(frozen=True)
class Evidence:
    offense: Offense
    packet: dict

    @property
    def evidence_id(self) -> str:
        return self.packet["evidence_id"]

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.packet, indent=indent, sort_keys=True)


def build_evidence(vote_1: Vote, vote_2: Vote,
                   snapshot_1: EpochSnapshot, snapshot_2: EpochSnapshot) -> Evidence:
    """Construct a self-contained evidence packet for a real conflict.

    ``snapshot_i`` is the target-epoch snapshot of ``vote_i``.  Weights at
    which the validator is slashable are taken from those snapshots.
    """
    if vote_1.validator_pubkey != vote_2.validator_pubkey:
        raise ValueError("evidence requires two votes from one validator")
    offense = classify_conflict(vote_1, vote_2)
    if offense is None:
        raise ValueError("votes do not constitute a defined offense")
    if snapshot_1.epoch != vote_1.target_epoch:
        raise ValueError("snapshot_1 must be the target-epoch snapshot of vote_1")
    if snapshot_2.epoch != vote_2.target_epoch:
        raise ValueError("snapshot_2 must be the target-epoch snapshot of vote_2")

    w1 = snapshot_1.weight_of(vote_1.validator_pubkey)
    w2 = snapshot_2.weight_of(vote_2.validator_pubkey)
    # One slashable mark per (validator, epoch): the validator's stake is not
    # summed twice when both votes target the same epoch (the double-vote case).
    weights: dict[int, int] = {}
    if snapshot_1.epoch == snapshot_2.epoch:
        w = max(w1, w2)
        if w > 0:
            weights[snapshot_1.epoch] = w
    else:
        if w1 > 0:
            weights[snapshot_1.epoch] = w1
        if w2 > 0:
            weights[snapshot_2.epoch] = w2

    packet = {
        "version": 1,
        "type": offense.value,
        "chain_id": vote_1.chain_id,
        "validator_pubkey": vote_1.validator_pubkey.hex(),
        "vote_1": vote_1.to_envelope(),
        "vote_2": vote_2.to_envelope(),
        "vote_1_snapshot": _snapshot_json(snapshot_1),
        "vote_2_snapshot": _snapshot_json(snapshot_2),
        "slashable_weight": {
            "epochs": [{"epoch": e, "weight": weights[e]} for e in sorted(weights)],
            "total_weight": sum(weights.values()),
        },
    }
    packet["evidence_id"] = evidence_id_of(packet)
    return Evidence(offense=offense, packet=packet)
