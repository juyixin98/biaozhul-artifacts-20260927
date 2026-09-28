"""In-memory lexicon model: trie index over a frozen dictionary version.

A :class:`LexiconVersion` is built once when a version is published/loaded
and never mutated; serving a request holds a reference to one immutable
version, so "requests already in progress are pinned to their version"
holds even while a newer version is being published.

Word costs are frequency based:

    cost = log((total_freq + alpha) / (word_freq + alpha))

frequent words are cheap; rare words are expensive but still below the
unknown-character fallback. Frequencies are part of the version payload.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

# Words longer than the longest lexicon word can never match; the DAG only
# needs to scan up to this many characters from each vertex.
MAX_WORD_LEN_CAP = 64


@dataclass(frozen=True)
class WordEntry:
    word: str
    freq: int


class TrieIndex:
    """Minimal trie used as a prefix dictionary over normalized text."""

    __slots__ = ("children", "terminal", "max_depth")

    def __init__(self) -> None:
        self.children: list[dict[str, int]] = [{}]
        self.terminal: list[bool] = [False]
        self.max_depth = 0

    def add(self, word: str) -> None:
        node = 0
        for i, ch in enumerate(word, start=1):
            nxt = self.children[node].get(ch)
            if nxt is None:
                nxt = len(self.children)
                self.children[node][ch] = nxt
                self.children.append({})
                self.terminal.append(False)
            node = nxt
            if i > self.max_depth:
                self.max_depth = i
        self.terminal[node] = True

    def prefix_matches(self, text: str, start: int, *, max_len: int) -> list[int]:
        """Return end offsets ``end`` (start < end) where text[start:end] is a word."""
        ends: list[int] = []
        node = 0
        upper = min(len(text), start + max_len)
        for i in range(start, upper):
            nxt = self.children[node].get(text[i])
            if nxt is None:
                break
            node = nxt
            if self.terminal[node]:
                ends.append(i + 1)
        return ends


def word_cost(freq: int, total_freq: int, *, alpha: float = 1.0) -> float:
    """Frequency-derived cost of emitting one dictionary word."""
    return math.log((total_freq + alpha) / (freq + alpha))


@dataclass(frozen=True)
class LexiconVersion:
    version_id: int
    entries: tuple[WordEntry, ...]
    trie: TrieIndex
    costs: dict[str, float]
    total_freq: int
    checksum: str

    @staticmethod
    def build(version_id: int, entries: Iterable[WordEntry]) -> "LexiconVersion":
        from .normalizer import normalize_word

        # De-duplicate by normalized form, keeping the highest frequency
        # stated for that form. Empty / whitespace-only words are rejected.
        by_word: dict[str, int] = {}
        for e in entries:
            w = normalize_word(e.word)
            if not w:
                raise ValueError("empty word after normalization")
            if len(w) > MAX_WORD_LEN_CAP:
                raise ValueError(f"word too long (>{MAX_WORD_LEN_CAP}): {w[:8]}...")
            if e.freq < 0:
                raise ValueError(f"negative frequency for word: {w[:8]}...")
            by_word[w] = max(by_word.get(w, 0), e.freq)

        ordered = tuple(sorted(by_word.items()))
        trie = TrieIndex()
        total_freq = 0
        for w, f in ordered:
            trie.add(w)
            total_freq += f
        costs = {w: word_cost(f, total_freq) for w, f in ordered}
        frozen_entries = tuple(WordEntry(w, f) for w, f in ordered)
        checksum = _checksum(version_id, ordered)
        return LexiconVersion(
            version_id=version_id,
            entries=frozen_entries,
            trie=trie,
            costs=costs,
            total_freq=total_freq,
            checksum=checksum,
        )

    def word_cost(self, word: str) -> float | None:
        return self.costs.get(word)

    @property
    def word_count(self) -> int:
        return len(self.entries)


def _checksum(version_id: int, ordered: tuple[tuple[str, int], ...]) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(f"v{version_id}\n".encode("utf-8"))
    for w, f in ordered:
        h.update(w.encode("utf-8"))
        h.update(b"\t")
        h.update(str(f).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:16]
