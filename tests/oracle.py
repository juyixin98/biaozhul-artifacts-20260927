"""Independent reference oracle used by tests ONLY.

This is deliberately written differently from the production core:

* segmentation candidates are found by *exhaustive recursion over every cut*
  (no trie, no DP, no shared code with ``app.core``);
* the cost of a word is recomputed directly as ``math.log(F / f)`` from the
  raw published frequencies -- the production pipeline is not consulted;
* expected answers below are hand-authored, not derived from the oracle alone.

It consumes exactly what a publish call consumes (a list of entries), so it
validates the whole pipeline through frequency -> cost -> DAG -> output.
"""
from __future__ import annotations

import math
import unicodedata
from typing import Optional


def oracle_fold(ch: str) -> str:
    # Mirrors the documented public policy, implemented independently.
    if ord(ch) in {0x00AD, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060}:
        return ""
    return unicodedata.normalize("NFKC", ch).casefold()


def oracle_normalize(text: str) -> str:
    return "".join(oracle_fold(c) for c in text)


class Oracle:
    """Brute-force best/second-best segmentation over a published entry list."""

    def __init__(self, entries: list[dict], unknown_char_cost: float = 8.0) -> None:
        # Build an independent normalized-key -> cost table.
        keys: dict[str, float] = {}
        total_freq = 0
        for e in entries:
            key = oracle_normalize(e["surface"])
            if e.get("cost") is not None:
                keys[key] = float(e["cost"])
            else:
                total_freq += int(e["frequency"])
        for e in entries:
            key = oracle_normalize(e["surface"])
            if e.get("cost") is None:
                keys[key] = math.log(total_freq / int(e["frequency"]))
        self.costs = keys
        self.unknown = unknown_char_cost

    def enumerate_paths(self, text: str) -> list[tuple[tuple[str, ...], float]]:
        """Every distinct segmentation of normalized ``text`` with its cost."""
        norm = oracle_normalize(text)
        results: list[tuple[tuple[str, ...], float]] = []

        def recurse(pos: int, acc: list[str], cost: float) -> None:
            if pos == len(norm):
                results.append((tuple(acc), cost))
                return
            single = norm[pos : pos + 1]
            single_is_word = single in self.costs
            # Dictionary matches of every length starting here.
            for end in range(pos + 1, len(norm) + 1):
                piece = norm[pos:end]
                if piece in self.costs:
                    recurse(end, acc + [piece], cost + self.costs[piece])
            # OOV policy: a single-character fallback edge exists ONLY when
            # that character is not itself a dictionary word. This mirrors the
            # production edge policy exactly (one fallback edge per boundary).
            if not single_is_word:
                recurse(pos + 1, acc + [single], cost + self.unknown)

        recurse(0, [], 0.0)

        # Dedup identical surface tuples (sum costs should agree).
        unique: dict[tuple[str, ...], float] = {}
        for surfaces, cost in results:
            if surfaces not in unique:
                unique[surfaces] = cost
        ranked = sorted(unique.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]))
        return ranked

    def best(self, text: str) -> tuple[tuple[str, ...], float]:
        return self.enumerate_paths(text)[0]

    def runner_up(self, text: str) -> Optional[tuple[tuple[str, ...], float]]:
        ranked = self.enumerate_paths(text)
        return ranked[1] if len(ranked) > 1 else None
