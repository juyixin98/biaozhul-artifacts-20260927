"""Exact minimum-displacement subtitle timing repair on an integer grid.

Every cue interval is a pair of integer variables (start, end) on a
millisecond-based grid. The solver minimises the total L1 displacement

    sum over cues of |start - start0| + |end - end0|

subject to:
  end - start >= min_duration_ms          (per cue)
  next.start - prev.end >= min_gap_ms     (cues in file order)
  0 <= start, end <= horizon              (media window or derived bound)

A dynamic program over grid positions (vectorised with NumPy) finds the exact
optimum. The solver never drops cues and never returns an infeasible
assignment: when the minimum required displacement exceeds the budget it
reports BUDGET_EXCEEDED with the required amount instead of squeezing cues
past their constraints, and when no feasible placement exists it reports
UNSOLVABLE.

Grid resolution: by default the solver runs on the gcd of all input times, so
results are exact. A coarser ``resolution_ms`` must divide that gcd, unless
``allow_approximate`` is set (then the placement grid is snapped and the trace
is flagged ``approximate``). ``max_grid`` bounds the grid size; beyond it the
solver reports TOO_LARGE rather than silently approximating.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

SOLVED = "SOLVED"
UNSOLVABLE = "UNSOLVABLE"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
TOO_LARGE = "TOO_LARGE"


class SolverInputError(Exception):
    """Invalid solver configuration (e.g. resolution not dividing the grid)."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class SolveResult:
    status: str
    minimal_change_ms: Optional[int]
    assignments: Optional[list]      # per cue (start_ms, end_ms), file order
    per_cue_change_ms: Optional[list]
    trace: dict


def _gcd_all(values) -> int:
    g = 0
    for v in values:
        g = math.gcd(g, abs(int(v)))
    return g


def solve_repair(intervals, *, min_duration_ms, min_gap_ms=0, media_duration_ms=None,
                 budget_ms=None, resolution_ms=None, allow_approximate=False,
                 max_grid=5000, block_mb=64) -> SolveResult:
    intervals = [(int(s), int(e)) for s, e in intervals]
    n = len(intervals)
    if min_duration_ms <= 0:
        raise SolverInputError("INVALID_CONFIG", "min_duration_ms must be positive")
    if min_gap_ms < 0:
        raise SolverInputError("INVALID_CONFIG", "min_gap_ms must be non-negative")
    if n == 0:
        return SolveResult(SOLVED, 0, [], [], {"cue_count": 0})

    packed = n * min_duration_ms + (n - 1) * min_gap_ms
    max_time = max(max(t) for t in intervals)
    if media_duration_ms is not None:
        if media_duration_ms < 0:
            raise SolverInputError("INVALID_CONFIG", "media_duration_ms must be non-negative")
        horizon = media_duration_ms
    else:
        # Upper bound: max original time + packed length. If an optimal chain
        # ended beyond this, every cue in its tight suffix chain would sit
        # past all original times, so shifting that chain 1ms earlier would
        # strictly reduce the cost -- contradiction. The packed layout from 0
        # also shows a feasible placement always exists within the bound.
        horizon = max_time + packed

    trace = {
        "cue_count": n,
        "horizon_ms": horizon,
        "packed_ms": packed,
        "media_duration_ms": media_duration_ms,
        "budget_ms": budget_ms,
    }

    if packed > horizon:
        trace["reason"] = (f"packed minimum layout needs {packed}ms but the "
                           f"horizon is {horizon}ms")
        return SolveResult(UNSOLVABLE, None, None, None, trace)

    g = _gcd_all([min_duration_ms, min_gap_ms, horizon]
                 + [t for iv in intervals for t in iv])
    approximate = False
    if resolution_ms is None:
        r = max(g, 1)
    else:
        r = int(resolution_ms)
        if r <= 0:
            raise SolverInputError("INVALID_RESOLUTION", "resolution_ms must be positive")
        if g == 0 or g % r != 0:
            if not allow_approximate:
                raise SolverInputError(
                    "INVALID_RESOLUTION",
                    f"resolution {r}ms does not divide the input time grid "
                    f"(gcd={g}ms); pass allow_approximate=true to solve on a "
                    "snapped grid",
                    {"gcd_ms": g, "resolution_ms": r},
                )
            approximate = True
    T = horizon // r
    trace.update({"resolution_ms": r, "grid_points": T + 1, "approximate": approximate})
    if T + 1 > max_grid:
        trace["max_grid"] = max_grid
        trace["reason"] = f"grid of {T + 1} points exceeds max_grid={max_grid}"
        return SolveResult(TOO_LARGE, None, None, None, trace)

    if approximate:
        dmin = math.ceil(min_duration_ms / r)
        gap = math.ceil(min_gap_ms / r)
    else:
        dmin = min_duration_ms // r
        gap = min_gap_ms // r

    idx = np.arange(T + 1)
    S = idx.astype(np.float64) * r  # ms position of each grid index
    INF = np.inf

    pm = np.zeros(T + 1)  # entering prefix minima; layer 0 has no predecessor
    enter_pa = np.full(T + 1, -1, dtype=np.int64)
    layers = []           # per layer: (argmin start per end, entering prefix argmin)
    layer_mins = []
    dp = None
    bsz = max(1, (block_mb * 1024 * 1024) // max(1, (T + 1) * 16))
    for (s0, e0) in intervals:
        base = np.abs(S - s0) + pm  # cost per candidate start, incl. best previous
        dp = np.full(T + 1, INF)
        arg = np.full(T + 1, -1, dtype=np.int64)
        e_idx = np.arange(dmin, T + 1)
        for lo in range(0, e_idx.size, bsz):
            E = e_idx[lo:lo + bsz]
            M = base[None, :] + np.abs(E[:, None] * r - e0)
            M[idx[None, :] > (E - dmin)[:, None]] = INF
            a = np.argmin(M, axis=1)
            dp[E] = M[np.arange(E.size), a]
            arg[E] = a
        layer_mins.append(None if not np.isfinite(dp.min()) else int(dp.min()))
        # prefix minima of dp, shifted right by gap, feed the next layer
        pm_new = np.minimum.accumulate(dp)
        is_new = np.empty(T + 1, dtype=bool)
        is_new[0] = True
        is_new[1:] = pm_new[1:] < pm_new[:-1]
        first_idx = np.flatnonzero(is_new)
        pa = first_idx[np.searchsorted(first_idx, idx, side="right") - 1]
        if gap > 0:
            pm_next = np.full(T + 1, INF)
            pm_next[gap:] = pm_new[:T + 1 - gap]
            pa_next = np.full(T + 1, -1, dtype=np.int64)
            pa_next[gap:] = pa[:T + 1 - gap]
        else:
            pm_next, pa_next = pm_new, pa
        layers.append((arg, enter_pa))
        pm, enter_pa = pm_next, pa_next

    total = dp.min()
    trace["layer_min_change_ms"] = layer_mins
    if not np.isfinite(total):
        trace["reason"] = "no feasible placement inside the horizon"
        return SolveResult(UNSOLVABLE, None, None, None, trace)
    total = int(total)
    trace["minimal_change_ms"] = total
    if budget_ms is not None and total > budget_ms:
        trace["reason"] = (f"minimal displacement {total}ms exceeds the "
                           f"budget {budget_ms}ms")
        return SolveResult(BUDGET_EXCEEDED, total, None, None, trace)

    # backtrack the optimal assignment
    assigns = [None] * n
    e = int(np.argmin(dp))
    for i in range(n - 1, -1, -1):
        arg, pa_in = layers[i]
        s = int(arg[e])
        assigns[i] = (s * r, e * r)
        if i > 0:
            e = int(pa_in[s])
    per_cue = [int(abs(ns - s0) + abs(ne - e0))
               for (ns, ne), (s0, e0) in zip(assigns, intervals)]
    return SolveResult(SOLVED, total, assigns, per_cue, trace)
