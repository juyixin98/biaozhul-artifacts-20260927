"""Test-only helpers.

The brute-force oracle here is deliberately written independently of
``app.algorithm``: it enumerates *every* edge path of the DAG with a plain
recursive walk, sums the documented cost rule itself, and returns the full
sorted list. The production implementation must agree with it. Shared with
the core is only the *cost formula* (``word_cost``) and the trie (word
membership — i.e. the test data, not the answer).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from app.lexicon import MAX_WORD_LEN_CAP, LexiconVersion, WordEntry


@dataclass(frozen=True)
class OraclePath:
    cost: float
    tokens: tuple[tuple[int, int, str, str], ...]  # (start,end,kind,surface)


def enumerate_all_paths(text: str, lex: LexiconVersion, unknown_char_cost: float) -> list[OraclePath]:
    """Exhaustively enumerate every complete path through the DAG."""
    n = len(text)
    results: list[OraclePath] = []

    def go(pos: int, acc: list, cost: float) -> None:
        if pos == n:
            results.append(OraclePath(cost, tuple(acc)))
            return
        # dictionary edges
        for end in lex.trie.prefix_matches(text, pos, max_len=MAX_WORD_LEN_CAP):
            surface = text[pos:end]
            go(end, acc + [(pos, end, "dict", surface)],
               cost + lex.costs[surface])
        # unknown single-character fallback edge
        go(pos + 1, acc + [(pos, pos + 1, "unknown", text[pos])],
           cost + unknown_char_cost)

    go(0, [], 0.0)
    # Apply the same documented stable tie-break (re-implemented independently
    # in tuple form), so index 1 is the canonical second-best path.
    results.sort(
        key=lambda p: (
            p.cost,
            tuple(t[3] for t in p.tokens),
            tuple(0 if t[2] == "dict" else 1 for t in p.tokens),
            len(p.tokens),
        )
    )
    return results


def dedupe_paths(paths: list[OraclePath]) -> list[OraclePath]:
    """Collapse paths with identical (start,end,kind) sequences."""
    seen: dict[tuple, OraclePath] = {}
    for p in paths:
        key = tuple((t[0], t[1], t[2]) for t in p.tokens)
        seen.setdefault(key, p)
    return sorted(
        seen.values(),
        key=lambda p: (
            p.cost,
            tuple(t[3] for t in p.tokens),
            tuple(0 if t[2] == "dict" else 1 for t in p.tokens),
            len(p.tokens),
        ),
    )


def build_lex(words: dict[str, int], version_id: int = 99) -> LexiconVersion:
    return LexiconVersion.build(
        version_id, [WordEntry(w, f) for w, f in words.items()]
    )


SEED_WORDS: dict[str, int] = {
    "研究": 5000, "研究生": 3000, "生命": 2000, "生": 800, "命": 300,
    "大学": 1500, "学": 700, "北京": 2000, "北京大学": 1200,
    "哈哈": 300, "哈": 100, "目的": 500, "目": 90, "的确": 400, "确": 100,
    "strasse": 100, "abc": 200, "cafe": 300, "coffee": 400,
    "自然": 850, "语言": 900, "处理": 950, "自然语言": 300,
}
