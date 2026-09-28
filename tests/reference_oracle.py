"""INDEPENDENT reference implementation for differential testing.

This is deliberately NOT a copy of the A* search under test.
It computes the same weighted adjacent-transposition distance by running
**Dijkstra over the space of strings**: every state is a concrete string,
edges are the four legal edit operations applied at every position. There is
no heuristic, priority bound logic or path code shared with ``app.core`` — if
both agree over thousands of random and exhaustive short pairs under varied
cost models, a systematic search bug is unlikely.

Bounded search (sound for the tested domain, documented for reviewers):

* The working alphabet is exactly ``chars(source) | chars(target)``. Any
  optimal script between the two strings only needs those symbols.
* An upper-bound budget is the cost of deleting the whole source then
  inserting the whole target, which is always a feasible script. Dijkstra
  never expands states past that budget.
* Intermediate string length is capped at ``len(source) + len(target)``.
  With non-negative costs an optimal script never needs longer temporary
  strings: surplus inserted symbols must later be deleted and cannot enable
  a permutation that adjacent transpositions cannot already produce.

Used only by tests; not imported by application code.
"""
from __future__ import annotations

import heapq
from typing import Iterable

from app.config import CostModel


def _neighbors(s: str, alphabet: tuple[str, ...], costs: CostModel, len_cap: int):
    for i, ch in enumerate(s):
        # delete
        yield s[:i] + s[i + 1:], ("del", i), costs.delete_cost(ch)
        # substitute (only to a different character)
        for r in alphabet:
            if r != ch:
                c = costs.substitute_cost(ch, r)
                yield s[:i] + r + s[i + 1:], ("sub", i, r), c
    # insert at every boundary
    if len(s) < len_cap:
        for i in range(len(s) + 1):
            for r in alphabet:
                yield s[:i] + r + s[i:], ("ins", i, r), costs.insert_cost(r)
    # adjacent swap (ordered source pair decides the cost -> asymmetric ok)
    for i in range(len(s) - 1):
        t = s[:i] + s[i + 1] + s[i] + s[i + 2:]
        yield t, ("tr", i), costs.transpose_cost(s[i], s[i + 1])


def _apply(source: str, script: Iterable[tuple]) -> str:
    s = source
    for step in script:
        kind = step[0]
        if kind == "del":
            i = step[1]
            s = s[:i] + s[i + 1:]
        elif kind == "ins":
            _, i, r = step
            s = s[:i] + r + s[i:]
        elif kind == "sub":
            _, i, r = step
            s = s[:i] + r + s[i + 1:]
        else:
            i = step[1]
            s = s[:i] + s[i + 1] + s[i] + s[i + 2:]
    return s


def _l1_heuristic(s: str, target: str, costs: CostModel) -> float:
    """Independent copy of the admissible directional length + freq bound."""
    from collections import Counter

    cs, ct = Counter(s), Counter(target)
    d1 = sum(abs(cs[c] - ct[c]) for c in set(cs) | set(ct))
    delta = len(s) - len(target)
    length_lb = delta * costs.min_delete if delta >= 0 else -delta * costs.min_insert
    return max(length_lb, 0.5 * d1 * costs.min_edit)


def reference_distance(
    source: str,
    target: str,
    costs: CostModel,
    *,
    threshold: float | None = None,
    node_cap: int = 1_000_000,
) -> float:
    """Exhaustive shortest-path distance; asserts the recorded path replays.

    With ``threshold`` set, returns ``inf`` when no script at or below the
    threshold exists (the f-pruning that enables this is admissible under
    non-negative costs), and the node cap keeps the bounded search finite.
    """
    if source == target:
        return 0.0

    alphabet = tuple(sorted(set(source) | set(target)))
    budget = (
        sum(costs.delete_cost(c) for c in source)
        + sum(costs.insert_cost(c) for c in target)
    )
    len_cap = len(source) + len(target)
    if threshold is not None:
        budget = min(budget, threshold)

    dist: dict[str, float] = {source: 0.0}
    prev: dict[str, tuple[str, tuple] | None] = {source: None}
    heap: list[tuple[float, int, str]] = [(0.0, 0, source)]
    tick = 0
    expanded = 0

    while heap:
        d, _, s = heapq.heappop(heap)
        if d != dist[s]:
            continue
        if s == target:
            # Reconstruct and self-check: the oracle's own path must replay.
            path: list[tuple] = []
            cur = s
            while prev[cur] is not None:
                parent, step = prev[cur]
                path.append(step)
                cur = parent
            path.reverse()
            assert _apply(source, path) == target, "oracle path failed self-check"
            return d
        if threshold is not None and d + _l1_heuristic(s, target, costs) > threshold + 1e-9:
            continue
        expanded += 1
        if expanded > node_cap:
            raise AssertionError(
                f"oracle node cap exceeded for {source!r}->{target!r}"
            )
        for t, step, w in _neighbors(s, alphabet, costs, len_cap):
            nd = d + w
            # Tolerance required: a feasible delete-all/insert-all path whose
            # exact cost equals the budget can overshoot by float error only.
            if nd > budget + 1e-9:
                continue
            if threshold is not None and nd + _l1_heuristic(t, target, costs) > threshold + 1e-9:
                continue
            if nd < dist.get(t, float("inf")):
                dist[t] = nd
                prev[t] = (s, step)
                tick += 1
                heapq.heappush(heap, (nd, tick, t))

    if threshold is not None:
        return float("inf")
    raise AssertionError(
        f"oracle exhausted its bounded search for {source!r}->{target!r}; "
        "length-cap or budget assumption violated"
    )
