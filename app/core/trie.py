"""Immutable in-memory trie over normalized dictionary surfaces."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass(frozen=True)
class WordEntry:
    """One immutable dictionary entry.

    Attributes:
        key: normalized lookup surface (the form matched against text).
        surface: surface as published (before normalization).
        cost: resolved positive cost.
        frequency: published frequency (0 when an explicit cost is used).
        explicit_cost: whether ``cost`` was given explicitly at publish time.
    """

    key: str
    surface: str
    cost: float
    frequency: int = 0
    explicit_cost: bool = False


@dataclass
class _Node:
    children: dict[str, "_Node"] = field(default_factory=dict)
    entry: Optional[WordEntry] = None


class Trie:
    """Read-only trie. No mutation methods are exposed after construction."""

    def __init__(self, entries: Iterator[WordEntry] | list[WordEntry] = ()) -> None:
        self._root = _Node()
        self._size = 0
        self._max_key_length = 0
        for entry in entries:
            self._insert(entry)

    def _insert(self, entry: WordEntry) -> None:
        node = self._root
        for ch in entry.key:
            nxt = node.children.get(ch)
            if nxt is None:
                nxt = _Node()
                node.children[ch] = nxt
            node = nxt
        if node.entry is None:
            self._size += 1
        # Last write wins; duplicate normalized keys are rejected earlier in
        # the publish pipeline, this just stays defensive.
        node.entry = entry
        self._max_key_length = max(self._max_key_length, len(entry.key))

    def __len__(self) -> int:
        return self._size

    @property
    def max_key_length(self) -> int:
        return self._max_key_length

    def get(self, key: str) -> Optional[WordEntry]:
        node = self._root
        for ch in key:
            node = node.children.get(ch)  # type: ignore[assignment]
            if node is None:
                return None
        return node.entry

    def matches_at(self, text: str, start: int, max_length: int) -> Iterator[WordEntry]:
        """Yield dictionary entries matching ``text`` starting at ``start``.

        Order: longest key first, then key lexicographic -- the order the DAG
        emits edges in. The trie walk stops at ``max_length`` chars.
        """
        node = self._root
        end = min(len(text), start + max_length)
        hits: list[WordEntry] = []
        for i in range(start, end):
            node = node.children.get(text[i])  # type: ignore[assignment]
            if node is None:
                break
            if node.entry is not None:
                hits.append(node.entry)
        hits.sort(key=lambda e: (-len(e.key), e.key))
        return iter(hits)
