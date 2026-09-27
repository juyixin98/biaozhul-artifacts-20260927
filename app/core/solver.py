"""Repair solver — minimum total time displacement under constraints.

Problem (integer milliseconds)
------------------------------
Given cues ordered by (start_ms, original index), repair only by *time shift*.
For cue i let ``s_i`` be the repaired start and ``d_i`` the repaired duration.

    minimize   sum_i |s_i - orig_start_i|      (start-shift part)
    subject to s_i + d_i + G <= s_{i+1}        (G = required gap, no overlap)
               L_i <= s_i <= R_i                (per-cue shift cap and
                                                 enclosing-segment box)
               d_i >= max(orig_dur_i, D_min)    (extend short cues only)
               d_i == orig_dur_i  otherwise     (never compress long cues)

The duration choice is fixed per cue: we extend a too-short/zero/flipped cue
to ``D_min`` when needed and never compress anything — the model is a *shift*
model, never a squeeze. That extension cost is unavoidable and reported
separately from the optimized start-shift cost.

Changing variables to

    y_i = s_i + sum_{j<i}(d_j + G)

turns the chain constraint into a monotone non-decreasing sequence, yielding a
**weighted L1 isotonic regression with per-point box constraints**:

    minimize sum_i |y_i - t_i|  s.t.  y non-decreasing and y_i in [lo_i, hi_i]

It is solved exactly by dynamic programming over the sorted set of candidate
values (targets and box endpoints), with numpy-vectorized relaxation per stage
and full backtracking. If the boxes are incompatible the result is
``infeasible_bounds``; a feasible placement whose start-shift cost exceeds the
configured total budget is ``budget_exceeded``. Neither case emits a repaired
document.
"""
from __future__ import annotations

import numpy as np

from ..logging_setup import get_logger
from .models import Cue, CueRepair, RepairPlan

log = get_logger("solver")

INF = 10**18
# Memory guard for the exact DP table (n stages x m candidates, int64).
_DP_MEMORY_BUDGET_BYTES = 256 * 1024 * 1024

_EXTEND_CODES = frozenset({"too_short", "zero_duration"})
_FLIP_CODE = "negative_duration"
_MOVE_CODES = frozenset(
    {"overlap", "same_start", "crosses_boundary",
     "starts_before_zero", "ends_past_horizon"}
)


def solve(
    cues: list[Cue],
    diagnostic_codes: dict[int, set[str]],
    *,
    min_duration_ms: int,
    max_duration_ms: int,
    min_gap_ms: int,
    segment_boundaries_ms: tuple[int, ...],
    horizon_ms: int,
    max_per_cue_shift_ms: int,
    max_total_shift_ms: int,
    run_id: str = "-",
) -> RepairPlan:
    n = len(cues)
    if n == 0:
        return RepairPlan("repaired", (), 0, 0, max_total_shift_ms, "no cues")

    # ---- 1. normalize negative-duration cues by flipping (recorded) --------
    norm_starts: list[int] = []
    flipped: set[int] = set()
    for c in cues:
        if c.duration_ms < 0 and _FLIP_CODE in diagnostic_codes.get(c.index, set()):
            flipped.add(c.index)
            norm_starts.append(c.end_ms)  # the earlier instant
        else:
            norm_starts.append(c.start_ms)

    # ---- 2. order by (normalized start, original index) --------------------
    order = sorted(range(n), key=lambda k: (norm_starts[k], k))

    # ---- 3. fixed-duration decisions: extension only, never compression ----
    d_star: list[int] = [0] * n
    extend_cost = 0
    for k in order:
        c = cues[k]
        codes = diagnostic_codes.get(c.index, set())
        orig_dur = -c.duration_ms if c.index in flipped else c.duration_ms
        needs_extend = bool(codes & _EXTEND_CODES) or (
            c.index in flipped and orig_dur < min_duration_ms
        )
        d = max(orig_dur, min_duration_ms) if needs_extend else orig_dur
        d_star[k] = d
        extend_cost += d - orig_dur

    # ---- 4. segment assignment & per-cue boxes -----------------------------
    edges = [0, *segment_boundaries_ms, horizon_ms]
    lo: list[int] = []
    hi: list[int] = []
    for k in order:
        c = cues[k]
        seg_idx = _segment_index(norm_starts[k], edges)
        if seg_idx is None:
            return _infeasible(
                max_total_shift_ms,
                f"cue #{c.index + 1} starts at {norm_starts[k]}ms outside the "
                f"content range [0, {horizon_ms}]",
            )
        seg_lo, seg_hi = edges[seg_idx], edges[seg_idx + 1]
        d = d_star[k]
        if d > seg_hi - seg_lo:
            return _infeasible(
                max_total_shift_ms,
                f"cue #{c.index + 1} needs {d}ms but segment "
                f"[{seg_lo},{seg_hi}) is only {seg_hi - seg_lo}ms long",
            )
        cap = max_per_cue_shift_ms
        # A cue must stay inside the segment it currently occupies (shifting a
        # cue into a different segment is itself a boundary violation), and the
        # movement is capped around its normalized start.
        box_lo = max(seg_lo, norm_starts[k] - cap)
        box_hi = min(seg_hi - d, norm_starts[k] + cap)
        if box_lo > box_hi:
            return _infeasible(
                max_total_shift_ms,
                f"cue #{c.index + 1} cannot fit in segment [{seg_lo},{seg_hi}) "
                f"within the +/-{cap}ms per-cue shift cap",
            )
        lo.append(box_lo)
        hi.append(box_hi)

    # ---- 5. transform to monotone coordinates ------------------------------
    # s_{i+1} >= s_i + d_i + G. With offset_i = sum_{j<i}(d_j + G) set
    # y_i = s_i - offset_i; then y_{i+1} >= y_i iff the chain holds.
    offset = np.zeros(n, dtype=np.int64)
    running = 0
    for pos in range(1, n):
        running += d_star[order[pos - 1]] + min_gap_ms
        offset[pos] = running
    lo_a = np.asarray(lo, dtype=np.int64) - offset
    hi_a = np.asarray(hi, dtype=np.int64) - offset
    target = np.asarray([norm_starts[k] for k in order], dtype=np.int64) - offset

    # Feasibility of monotone boxes: prefix maxima of lo must stay within hi.
    max_lo = np.maximum.accumulate(lo_a)
    if np.any(max_lo > hi_a):
        bad = int(np.argmax(max_lo > hi_a))
        return _infeasible(
            max_total_shift_ms,
            f"segments/caps leave no monotone placement for "
            f"cue #{cues[order[bad]].index + 1} (chain infeasible)",
        )

    # ---- 6. exact isotonic DP: backward table g ----------------------------
    # g[i, j] = min cost of stages i..n-1 with y_i fixed at candidates[j].
    # Feasible transitions are non-decreasing: at stage i a successor must use
    # a candidate index >= j.
    candidates = np.unique(np.concatenate([target, lo_a, hi_a]))
    m = candidates.size
    # one int64 table of n*m
    if 8 * n * m > _DP_MEMORY_BUDGET_BYTES:
        return RepairPlan(
            status="solver_too_large",
            cues=(),
            total_shift_ms=0,
            max_shift_ms=0,
            budget_ms=max_total_shift_ms,
            message=(f"exact DP needs an {n}x{m} table (> "
                     f"{_DP_MEMORY_BUDGET_BYTES // (1024 * 1024)}MB cap); "
                     f"split the document into smaller jobs"),
        )
    log.info("[%s] DP start: n=%d candidates=%d mandatory_extend=%dms",
             run_id, n, m, extend_cost)

    local = np.abs(candidates[None, :] - target[:, None])  # (n, m)
    feasible = (candidates[None, :] >= lo_a[:, None]) & \
               (candidates[None, :] <= hi_a[:, None])

    g = np.full((n, m), INF, dtype=np.int64)
    g[n - 1] = np.where(feasible[n - 1], local[n - 1], INF)
    for i in range(n - 2, -1, -1):
        suffix_min = np.minimum.accumulate(g[i + 1][::-1])[::-1]
        g[i] = np.where(feasible[i], local[i] + suffix_min, INF)

    total_start_shift = int(g[0].min())
    log.info("[%s] DP done: total_start_shift=%dms (budget %dms) extend=%dms",
             run_id, total_start_shift, max_total_shift_ms, extend_cost)

    if total_start_shift > max_total_shift_ms:
        return RepairPlan(
            status="budget_exceeded",
            cues=(),
            total_shift_ms=total_start_shift,
            max_shift_ms=0,
            budget_ms=max_total_shift_ms,
            message=(f"minimum total start displacement {total_start_shift}ms "
                     f"exceeds budget {max_total_shift_ms}ms; refusing to force a "
                     f"squeeze (mandatory duration extension is {extend_cost}ms)"),
        )

    # ---- 7. lexicographic reconstruction ------------------------------------
    # Walk cues in order. ``remaining`` is the optimal cost still available to
    # stages i..n-1 given the fixed prefix (monotonicity bound prev_idx). A
    # candidate is acceptable iff the suffix DP can still attain ``remaining``
    # through it (need == remaining).
    #
    # Selection per cue: stay at the cue's original target whenever some
    # globally optimal placement allows it (zero displacement); otherwise pick
    # the feasible optimum closest to the target, tie-breaking toward the
    # smaller value. Thus each cue is held unless the chain forces a move.
    chosen_idx = np.empty(n, dtype=np.int64)
    prev_idx = 0
    remaining = total_start_shift
    arange_m = np.arange(m)
    for i in range(n):
        if i < n - 1:
            suffix_min = np.minimum.accumulate(g[i + 1][::-1])[::-1]
            need = local[i] + suffix_min
        else:
            need = local[i]
        good = feasible[i] & (arange_m >= prev_idx) & (need == remaining)
        if not good.any():
            return _infeasible(max_total_shift_ms,
                               "DP reconstruction found no optimal cell (internal)")
        target_idx = int(np.searchsorted(candidates, target[i]))
        if target_idx < m and candidates[target_idx] == target[i] \
                and good[target_idx]:
            k = target_idx                              # stay put if possible
        else:
            opts = np.flatnonzero(good)
            # nearest candidate to the target; tie -> the smaller (upstream)
            nearest = opts[np.argmin(np.abs(candidates[opts] - target[i]))]
            ties = opts[np.abs(candidates[opts] - target[i])
                         == abs(int(candidates[nearest]) - int(target[i]))]
            k = int(ties[0])
        chosen_idx[i] = k
        prev_idx = k
        remaining = int(suffix_min[k]) if i < n - 1 else 0

    chosen = candidates[chosen_idx]

    s_sorted = chosen + offset
    repairs: list[CueRepair] = []
    for pos, k in enumerate(order):
        c = cues[k]
        new_start = int(s_sorted[pos])
        new_end = new_start + d_star[k]
        codes = diagnostic_codes.get(c.index, set())
        # Movement is measured from the normalized start: a flipped cue's
        # baseline is the earlier instant; the raw end-before-start value is
        # preserved verbatim in original_start_ms/original_end_ms.
        baseline_start = c.end_ms if c.index in flipped else c.start_ms
        shift = new_start - baseline_start
        reasons = tuple(sorted(codes & (_MOVE_CODES | _EXTEND_CODES | {_FLIP_CODE})))
        if shift != 0 and "overlap" not in reasons:
            # Moved only because an upstream repair pushed this cue downstream.
            reasons = reasons + ("chain_propagation",)
        was_extended = d_star[k] != (-c.duration_ms if c.index in flipped
                                     else c.duration_ms)
        if c.index in flipped:
            action = "flipped+moved" if shift != 0 else "flipped"
        elif was_extended:
            action = "extended+moved" if shift != 0 else "extended"
        elif shift != 0:
            action = "moved"
        else:
            action = "held"
        repairs.append(
            CueRepair(
                cue_index=c.index,
                original_start_ms=c.start_ms,
                original_end_ms=c.end_ms,
                repaired_start_ms=new_start,
                repaired_end_ms=new_end,
                shift_ms=shift,
                reasons=reasons,
                action=action,
            )
        )
    repairs.sort(key=lambda r: r.cue_index)
    return RepairPlan(
        status="repaired",
        cues=tuple(repairs),
        total_shift_ms=total_start_shift,
        max_shift_ms=max(abs(r.shift_ms) for r in repairs),
        budget_ms=max_total_shift_ms,
        message=f"optimal start displacement {total_start_shift}ms; "
        f"mandatory duration extension {extend_cost}ms",
    )


def _segment_index(t: int, edges: list[int]) -> int | None:
    if t < edges[0] or t >= edges[-1]:
        return None
    for i in range(len(edges) - 1):
        if edges[i] <= t < edges[i + 1]:
            return i
    return None


def _infeasible(budget: int, message: str) -> RepairPlan:
    log.info("solver infeasible: %s", message)
    return RepairPlan(
        status="infeasible_bounds",
        cues=(),
        total_shift_ms=0,
        max_shift_ms=0,
        budget_ms=budget,
        message=message,
    )
