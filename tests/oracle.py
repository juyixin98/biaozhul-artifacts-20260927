"""Independent brute-force / DP oracle for the repair optimization.

The reference solver is written from scratch and does NOT import or reuse
:mod:`app.core.solver`. Two independent implementations are provided:

* :func:`oracle_min_shift` — exhaustive bound-pruned DFS enumeration over a
  candidate grid (used when the grid is small; verifies the *argmin*).
* :func:`oracle_min_shift_dp` — a self-contained vectorized DP over a dense
  integer window (used for larger randomized cross-checks).

Both share no code with the production solver beyond the problem data
(integer milliseconds) and return the true minimum total start displacement.
"""
from __future__ import annotations

import numpy as np

INF = 10**18


def oracle_min_shift(
    orig_starts: list[int],
    durations: list[int],
    boxes: list[tuple[int, int]],
    *,
    min_gap_ms: int = 1,
    grid: list[int] | None = None,
) -> tuple[int, list[int]]:
    """DFS oracle: ``(minimum sum|s_i-orig_i|, argmin starts)``.

    Feasibility: ``s_{i+1} >= s_i + durations[i] + min_gap_ms`` and each start
    inside its inclusive box.
    """
    n = len(orig_starts)
    if n == 0:
        return 0, []
    columns: list[list[int]] = []
    for i in range(n):
        lo, hi = boxes[i]
        if lo > hi:
            return INF, []
        cols = list(range(lo, hi + 1)) if grid is None else [v for v in grid
                                                              if lo <= v <= hi]
        if not cols:
            return INF, []
        columns.append(cols)

    best = INF
    best_place: list[int] = []
    place = [0] * n

    def recurse(i: int, min_start: int, cost: int) -> None:
        nonlocal best, best_place
        if cost >= best:
            return
        if i == n:
            best, best_place = cost, place.copy()
            return
        lo, _ = boxes[i]
        start_lo = max(min_start, lo)
        for s in columns[i]:
            if s < start_lo:
                continue
            new_cost = cost + abs(s - orig_starts[i])
            if new_cost >= best:
                continue
            place[i] = s
            recurse(i + 1, s + durations[i] + min_gap_ms, new_cost)

    recurse(0, -INF, 0)
    return (INF, []) if best == INF else (best, best_place)


def oracle_min_shift_dp(
    orig_starts: list[int],
    durations: list[int],
    boxes: list[tuple[int, int]],
    *,
    min_gap_ms: int = 1,
    window_hi: int | None = None,
) -> int:
    """Dense integer-millisecond DP oracle; returns only the optimum cost.

    Written independently with numpy: stage DP over every millisecond in the
    feasible window. ``INF`` means infeasible. The implementation differs from
    the production solver (dense full-window table, no candidate compression).
    """
    n = len(orig_starts)
    if n == 0:
        return 0
    hi = window_hi if window_hi is not None else max(b[1] for b in boxes)
    t = np.arange(0, hi + 1, dtype=np.int64)
    prev = np.full(hi + 1, INF, dtype=np.int64)
    lo0, hi0 = boxes[0]
    prev[lo0:hi0 + 1] = np.abs(t[lo0:hi0 + 1] - orig_starts[0])
    for i in range(1, n):
        # non-decreasing transition: predecessor time <= t - step
        step = durations[i - 1] + min_gap_ms
        cum = np.minimum.accumulate(prev)
        allowed = np.full(hi + 1, INF, dtype=np.int64)
        if step <= hi:
            allowed[step:] = cum[: hi + 1 - step]
        lo_i, hi_i = boxes[i]
        cur = np.full(hi + 1, INF, dtype=np.int64)
        cur[lo_i:hi_i + 1] = (
            allowed[lo_i:hi_i + 1]
            + np.abs(t[lo_i:hi_i + 1] - orig_starts[i])
        )
        prev = cur
    out = int(prev.min())
    return INF if out >= INF else out
