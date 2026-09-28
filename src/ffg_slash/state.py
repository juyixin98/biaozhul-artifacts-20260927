"""Pure chain-state kernel: supermajority links and simplified finality.

Explicit simplified finality rule (no real fork choice):

* A *supermajority link* ``s -> t`` is formed when votes on it reach
  ``weight(s,t) * 3 >= total_weight(snapshot[t]) * 2`` (strict 2/3).
* A link *justifies* ``t`` only when its source is the currently justified
  checkpoint and ``t == justified.epoch + 1`` (consecutive epochs).
* Justifying ``t`` *finalizes* the previously justified checkpoint (the
  source ``s``). Non-consecutive links are recorded ("dangling") but neither
  justify nor finalize until they become relevant — impossible under the
  consecutive rule, which is exactly the simplification being made explicit.

Weights always come from the target-epoch snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .registry import EpochSnapshot, ValidatorRegistry


@dataclass(frozen=True)
class Checkpoint:
    epoch: int
    root: bytes

    def as_dict(self) -> dict:
        return {"epoch": self.epoch, "root": self.root.hex()}


def link_key(source_epoch: int, source_root: bytes,
             target_epoch: int, target_root: bytes) -> tuple:
    return (source_epoch, bytes(source_root), target_epoch, bytes(target_root))


def reaches_quorum(link_weight: int, snapshot: EpochSnapshot) -> bool:
    total = snapshot.total_weight()
    if total <= 0:
        return False
    return link_weight * 3 >= total * 2


@dataclass
class LinkAccumulator:
    """Tracks weight on each (source -> target) link for one epoch view."""

    weights: dict[tuple, int] = field(default_factory=dict)
    voters: dict[tuple, set[bytes]] = field(default_factory=dict)

    def add(self, key: tuple, pubkey: bytes, weight: int) -> int:
        """One validator counts at most once per link. Returns new total."""
        seen = self.voters.setdefault(key, set())
        if pubkey in seen:
            return self.weights.get(key, 0)
        seen.add(pubkey)
        self.weights[key] = self.weights.get(key, 0) + weight
        return self.weights[key]

    def weight_on(self, key: tuple) -> int:
        return self.weights.get(key, 0)


@dataclass
class FinalityTransition:
    justified: Checkpoint | None
    finalized: Checkpoint | None
    link_weight: int
    total_weight: int
    quorum: bool


class FinalityState:
    """Mutable justified/finalized cursor, driven by accepted valid votes."""

    def __init__(self, registry: ValidatorRegistry, genesis_root: bytes):
        self.registry = registry
        self.genesis_root = bytes(genesis_root)
        self.links = LinkAccumulator()
        # Epoch 0 / genesis is justified by definition.
        self.justified = Checkpoint(0, self.genesis_root)
        self.finalized: Checkpoint | None = None
        self.justified_history: list[Checkpoint] = [self.justified]
        self.finalized_history: list[Checkpoint] = []
        self.formed_links: set[tuple] = set()  # once a link hits quorum

    def apply_vote(self, pubkey: bytes, source_epoch: int, source_root: bytes,
                   target_epoch: int, target_root: bytes) -> FinalityTransition:
        """Fold one validated membership vote into the finality kernel."""
        snapshot = self.registry.snapshot(target_epoch)
        weight = snapshot.weight_of(pubkey)
        key = link_key(source_epoch, source_root, target_epoch, target_root)
        new_weight = self.links.add(key, pubkey, weight)

        quorum = reaches_quorum(new_weight, snapshot)
        out_justified = out_finalized = None
        if quorum and key not in self.formed_links:
            self.formed_links.add(key)
            # Simplified rule: consecutive source == current justified cp.
            if (source_epoch == self.justified.epoch
                    and bytes(source_root) == self.justified.root
                    and target_epoch == self.justified.epoch + 1):
                target_cp = Checkpoint(target_epoch, bytes(target_root))
                finalized_cp = self.justified
                self.finalized = finalized_cp
                self.justified = target_cp
                self.finalized_history.append(finalized_cp)
                self.justified_history.append(target_cp)
                out_justified = target_cp
                out_finalized = finalized_cp
        return FinalityTransition(
            justified=out_justified,
            finalized=out_finalized,
            link_weight=new_weight,
            total_weight=snapshot.total_weight(),
            quorum=quorum,
        )

    def snapshot_state(self) -> dict:
        return {
            "justified": self.justified.as_dict(),
            "finalized": self.finalized.as_dict() if self.finalized else None,
            "formed_links": len(self.formed_links),
        }
