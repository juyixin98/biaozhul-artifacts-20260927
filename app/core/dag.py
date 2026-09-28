"""DAG construction over normalized text.

Every vertex is a character boundary ``0..n``. Every edge ``i -> j`` is one
segmentation token spanning ``text[i:j]``. Two edge kinds exist:

* ``dict``    -- a dictionary/trie match carrying the word cost;
* ``unknown`` -- a fallback edge. For every boundary there is exactly one
                 fallback edge covering the next normalized character, and it
                 is emitted **only when that character is not itself a
                 dictionary word**.  Cost is ``unknown_char_cost``.

The fallback rule guarantees:

* full coverage -- every boundary has an outgoing edge until ``n``, so no
  character can ever be dropped;
* fixed fallback length -- always one normalized character;
* a single, deterministic edge order: longer words first, then lexicographic
  surface, with the unknown edge last. That makes DP tie handling independent
  of insertion order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .trie import Trie

UNKNOWN_KIND = "unknown"
DICT_KIND = "dict"


@dataclass(frozen=True)
class Edge:
    start: int
    end: int
    cost: float
    kind: str
    surface: str          # matched normalized substring
    display: str          # dictionary surface as published; same as surface for OOV


def iter_dag_edges(text: str, trie: Trie, unknown_char_cost: float, max_word_length: int) -> Iterator[Edge]:
    """Yield all DAG edges, grouped by ``start`` and in deterministic order."""
    n = len(text)
    for start in range(n):
        emitted_ends: set[int] = set()
        dict_surfaces: list[str] = []
        for entry in trie.matches_at(text, start, max_word_length):
            end = start + len(entry.key)
            emitted_ends.add(end)
            dict_surfaces.append(entry.key)
            yield Edge(
                start=start,
                end=end,
                cost=entry.cost,
                kind=DICT_KIND,
                surface=entry.key,
                display=entry.surface,
            )
        # Deterministic single-character OOV fallback. Skipped when the
        # character is a known dictionary word (its dict edge already exists).
        if (start + 1) not in emitted_ends:
            ch = text[start : start + 1]
            yield Edge(
                start=start,
                end=start + 1,
                cost=unknown_char_cost,
                kind=UNKNOWN_KIND,
                surface=ch,
                display=ch,
            )
