"""Validator registry: per-epoch weight snapshots.

Weights for every vote and every piece of evidence are taken from the snapshot
of the *target epoch* of the vote — never from the current/latest set, so
evidence stays valid after membership changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .encoding import PUBKEY_SIZE, snapshot_root


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class EpochSnapshot:
    epoch: int
    chain_id: int
    members: dict[bytes, int]  # pubkey -> weight

    def total_weight(self) -> int:
        return sum(self.members.values())

    def weight_of(self, pubkey: bytes) -> int:
        return self.members.get(pubkey, 0)

    def is_member(self, pubkey: bytes) -> bool:
        return pubkey in self.members

    def root(self) -> bytes:
        return snapshot_root(epoch=self.epoch, chain_id=self.chain_id,
                             members=list(self.members.items()))

    def member_list(self) -> list[tuple[bytes, int]]:
        return sorted(self.members.items(), key=lambda m: m[0])


class ValidatorRegistry:
    """Holds explicit snapshots; an epoch with no snapshot has no voters."""

    def __init__(self, chain_id: int):
        self.chain_id = chain_id
        self._snapshots: dict[int, EpochSnapshot] = {}

    def add_epoch(self, epoch: int, members: dict[bytes, int]) -> EpochSnapshot:
        if epoch < 0:
            raise RegistryError("epoch must be non-negative")
        clean: dict[bytes, int] = {}
        for pubkey, weight in members.items():
            if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != PUBKEY_SIZE:
                raise RegistryError("validator pubkey must be 32 bytes")
            if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
                raise RegistryError("weight must be a positive integer")
            clean[bytes(pubkey)] = weight
        snap = EpochSnapshot(epoch=epoch, chain_id=self.chain_id, members=clean)
        self._snapshots[epoch] = snap
        return snap

    def has_snapshot(self, epoch: int) -> bool:
        return epoch in self._snapshots

    def snapshot(self, epoch: int) -> EpochSnapshot:
        try:
            return self._snapshots[epoch]
        except KeyError as exc:
            raise RegistryError(f"no validator snapshot for epoch {epoch}") from exc

    def epochs(self) -> list[int]:
        return sorted(self._snapshots)

    def is_active(self, pubkey: bytes, epoch: int) -> bool:
        snap = self._snapshots.get(epoch)
        return snap is not None and snap.is_member(pubkey)
