"""Validator set with epoch snapshots.

Weights are ALWAYS taken from the epoch snapshot a round belongs to — never
from a "current" set that changes underneath historical votes. The schedule
is explicit and simple:

    epoch_index(round) = round // epoch_length

A validator's weight is a function of epoch: the registry stores a sorted
list of (effective_epoch, weight) updates per validator plus its pubkey.
Weight 0 (or absence) at an epoch means the validator is not a member there
(exited / not yet joined) — this drives the membership-change tests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ValidatorRecord:
    validator_id: str
    pubkey: bytes
    # Sorted, non-overlapping updates: [(effective_epoch, weight), ...]
    weight_updates: tuple[tuple[int, int], ...]


class ValidatorRegistry:
    def __init__(self, chain_id: str, epoch_length: int):
        if epoch_length <= 0:
            raise ValueError("epoch_length must be positive")
        self.chain_id = chain_id
        self.epoch_length = epoch_length
        self._validators: dict[str, ValidatorRecord] = {}

    # -- construction ----------------------------------------------------- #

    def add_validator(self, validator_id: str, pubkey: bytes, weight: int, effective_epoch: int = 0) -> None:
        if validator_id in self._validators:
            raise ValueError(f"validator already registered: {validator_id}")
        if weight < 0:
            raise ValueError("weight must be non-negative")
        if effective_epoch < 0:
            raise ValueError("effective_epoch must be non-negative")
        self._validators[validator_id] = ValidatorRecord(
            validator_id=validator_id,
            pubkey=bytes(pubkey),
            weight_updates=((effective_epoch, weight),),
        )

    def set_weight(self, validator_id: str, effective_epoch: int, weight: int) -> None:
        rec = self._validators.get(validator_id)
        if rec is None:
            raise KeyError(f"unknown validator: {validator_id}")
        if weight < 0 or effective_epoch < 0:
            raise ValueError("weight and epoch must be non-negative")
        updates = list(rec.weight_updates)
        # overwrite if an update at same epoch exists, else insert ordered
        updates = [(e, w) for (e, w) in updates if e != effective_epoch]
        updates.append((effective_epoch, weight))
        updates.sort()
        self._validators[validator_id] = ValidatorRecord(rec.validator_id, rec.pubkey, tuple(updates))

    # -- queries ---------------------------------------------------------- #

    def epoch_of_round(self, round_index: int) -> int:
        if round_index < 0:
            raise ValueError("round must be non-negative")
        return round_index // self.epoch_length

    def exists(self, validator_id: str) -> bool:
        return validator_id in self._validators

    def pubkey_of(self, validator_id: str) -> bytes | None:
        rec = self._validators.get(validator_id)
        return None if rec is None else rec.pubkey

    def weight_at_epoch(self, validator_id: str, epoch: int) -> int | None:
        """Weight at a given epoch.

        Returns None for an unknown validator; 0 for a known validator that
        is not an active member at that epoch.
        """
        rec = self._validators.get(validator_id)
        if rec is None:
            return None
        active_weight = 0
        for effective_epoch, w in rec.weight_updates:
            if effective_epoch <= epoch:
                active_weight = w
            else:
                break
        return active_weight

    def weight_at_round(self, validator_id: str, round_index: int) -> int | None:
        return self.weight_at_epoch(validator_id, self.epoch_of_round(round_index))

    def is_active_at_round(self, validator_id: str, round_index: int) -> bool:
        w = self.weight_at_round(validator_id, round_index)
        return w is not None and w > 0

    def snapshot_at_epoch(self, epoch: int) -> dict[str, int]:
        """Active {validator_id: weight} at an epoch (members with weight > 0)."""
        snap: dict[str, int] = {}
        for vid in self._validators:
            w = self.weight_at_epoch(vid, epoch)
            if w and w > 0:
                snap[vid] = w
        return snap

    def total_active_weight_at_epoch(self, epoch: int) -> int:
        return sum(self.snapshot_at_epoch(epoch).values())

    # -- serialization (fixtures / replay context) ------------------------ #

    def to_json(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "epoch_length": self.epoch_length,
            "validators": [
                {
                    "validator_id": rec.validator_id,
                    "pubkey": rec.pubkey.hex(),
                    "weight_updates": [list(u) for u in rec.weight_updates],
                }
                for rec in sorted(self._validators.values(), key=lambda r: r.validator_id)
            ],
        }

    @classmethod
    def from_json(cls, data: dict) -> "ValidatorRegistry":
        reg = cls(chain_id=str(data["chain_id"]), epoch_length=int(data["epoch_length"]))
        for v in data.get("validators", []):
            rec = ValidatorRecord(
                validator_id=str(v["validator_id"]),
                pubkey=bytes.fromhex(v["pubkey"]),
                weight_updates=tuple((int(e), int(w)) for e, w in v["weight_updates"]),
            )
            reg._validators[rec.validator_id] = rec
        return reg

    def export_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, indent=2, sort_keys=True)
