"""An immutable, queryable snapshot of one published dictionary version."""
from __future__ import annotations

from dataclasses import dataclass

from ..core.cost import FrequencyBasis
from ..core.trie import Trie, WordEntry
from .repository import StoredEntry, VersionInfo


@dataclass(frozen=True)
class Snapshot:
    """Everything a segmentation request needs -- never changes after build."""

    version: str
    info: VersionInfo
    trie: Trie
    total_frequency: int

    def word_count(self) -> int:
        return len(self.trie)


def build_snapshot(info: VersionInfo, stored: list[StoredEntry], min_word_cost: float) -> Snapshot:
    """Build resolved entries (frequency -> log cost) and the immutable trie."""
    basis = FrequencyBasis(total_frequency=info.total_frequency, word_count=len(stored))
    words: list[WordEntry] = []
    for row in stored:
        if row.explicit_cost and row.cost_value is not None:
            cost = max(row.cost_value, min_word_cost)
            entry = WordEntry(
                key=row.key, surface=row.surface, cost=cost,
                frequency=row.frequency, explicit_cost=True,
            )
        else:
            cost = basis.word_cost(row.frequency, min_word_cost=min_word_cost)
            entry = WordEntry(
                key=row.key, surface=row.surface, cost=cost,
                frequency=row.frequency, explicit_cost=False,
            )
        words.append(entry)
    trie = Trie(words)
    return Snapshot(version=info.version, info=info, trie=trie,
                    total_frequency=info.total_frequency)
