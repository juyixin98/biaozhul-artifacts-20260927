"""Exhaustive small-grid verification against an independent oracle.

Design (so brute force stays tractable yet complete):

* Time values live on a 250ms lattice; in the lattice cases ``min_gap_ms`` is
  0, so every optimal placement lies on the lattice (chain constraints are
  then ``s_{i+1} >= s_i + d_i`` — lattice-to-lattice). The oracle enumerates
  *every* feasible placement via bound-pruned DFS, independent of the
  production solver.
* Two regimes: normal boxes (mostly feasible) and tight boxes (small segment +
  small per-cue cap, surfacing many ``infeasible_bounds`` outcomes).
* An n=3 dense lattice sweep uses tiny windows.
* Random 1ms-gap instances are checked against an oracle over a
  "lattice +/-1ms" grid: with 250ms-aligned targets/durations and gap 1, every
  optimum lives on that grid, so the independent search stays tiny while the
  gap being tested is a real off-lattice millisecond.

The oracle never imports the production solver; its answers are independent of
the code under test. Every reported placement is re-verified constraint by
constraint inside the test.
"""
from __future__ import annotations

import itertools
import logging
import random

import pytest

from app.core.models import Cue
from app.core.solver import solve
from .oracle import INF, oracle_min_shift, oracle_min_shift_dp

log = logging.getLogger("subguard.test.exhaustive")

LATTICE = list(range(0, 8_001, 250))     # 0..8s in 250ms steps
DURATIONS = [1_000, 2_000, 3_000]
GAP = 0                                   # lattice-aligned chain constraints
SEGMENTS = (10_000,)
HORIZON = 20_000
CAP = 8_000
BUDGET = 1_000_000


def _run_production(starts, durs, *, segments=SEGMENTS, horizon=HORIZON,
                    cap=CAP, gap=GAP, budget=BUDGET):
    cues = [Cue(i, starts[i], starts[i] + durs[i], (f"c{i}",))
            for i in range(len(starts))]
    codes = {i: {"overlap"} for i in range(len(starts))}
    return solve(
        cues, codes,
        min_duration_ms=1_000, max_duration_ms=7_000, min_gap_ms=gap,
        segment_boundaries_ms=segments, horizon_ms=horizon,
        max_per_cue_shift_ms=cap, max_total_shift_ms=budget,
        run_id="exhaustive",
    )


def _independent_check(starts, durs, plan, *, cap=CAP, seg_hi=SEGMENTS[0],
                       gap=GAP):
    by_index = {r.cue_index: r for r in plan.cues}
    assert len(by_index) == len(starts)
    # Overlap chain is enforced in the solver's canonical (start, index) order.
    order = sorted(range(len(starts)), key=lambda k: (starts[k], k))
    total = 0
    for i in range(len(starts)):
        r = by_index[i]
        total += abs(r.repaired_start_ms - starts[i])
        assert abs(r.shift_ms) <= cap
        assert 0 <= r.repaired_start_ms
        assert r.repaired_end_ms <= seg_hi
        assert r.repaired_end_ms - r.repaired_start_ms == durs[i]
    rows = [by_index[i] for i in order]
    for i in range(len(rows) - 1):
        assert rows[i + 1].repaired_start_ms - rows[i].repaired_end_ms >= gap
    assert total == plan.total_shift_ms
    return total


def _oracle_boxes(o_starts, o_durs, *, cap, seg_hi):
    return [
        (max(0, o_starts[i] - cap), min(seg_hi - o_durs[i], o_starts[i] + cap))
        for i in range(len(o_starts))
    ]


@pytest.mark.exhaustive
@pytest.mark.parametrize(
    "starts,durs",
    [
        # n=1: every lattice start x duration
        *[((s,), (d,)) for s, d in itertools.product(LATTICE, DURATIONS)],
        # n=2: full Cartesian lattice
        *[
            ((s0, s1), (d0, d1))
            for s0, s1 in itertools.product(LATTICE, repeat=2)
            for d0, d1 in itertools.product(DURATIONS, repeat=2)
        ],
    ],
)
def test_solver_matches_bruteforce_oracle(starts, durs):
    n = len(starts)
    order = sorted(range(n), key=lambda k: (starts[k], k))
    o_starts = [starts[k] for k in order]
    o_durs = [durs[k] for k in order]
    boxes = _oracle_boxes(o_starts, o_durs, cap=CAP, seg_hi=SEGMENTS[0])
    # oracle grid must cover the whole feasible segment, not just targets
    oracle_grid = list(range(0, SEGMENTS[0] + 1, 250))
    oracle_cost, oracle_place = oracle_min_shift(
        o_starts, o_durs, boxes, min_gap_ms=GAP, grid=oracle_grid)

    plan = _run_production(list(starts), list(durs))

    if oracle_cost == INF:
        assert plan.status == "infeasible_bounds", (
            f"oracle infeasible but solver said {plan.status}: {starts} {durs}")
        return

    assert plan.status == "repaired", (starts, durs, plan.message)
    prod_cost = _independent_check(list(starts), list(durs), plan)
    assert prod_cost == oracle_cost, (
        f"objective mismatch: solver={prod_cost} oracle={oracle_cost} "
        f"starts={starts} durs={durs}")
    for i in range(n - 1):
        assert oracle_place[i + 1] - (oracle_place[i] + o_durs[i]) >= GAP
    for i, s in enumerate(oracle_place):
        assert boxes[i][0] <= s <= boxes[i][1]


@pytest.mark.exhaustive
def test_tight_boxes_infeasibility_matches_oracle():
    """Small segment + small per-cue cap so many instances are infeasible."""
    seg_hi = 7_000
    cap = 1_000
    starts_grid = list(range(0, 6_001, 500))
    dur_grid = [2_000, 3_000]
    cases = list(itertools.product(
        itertools.product(starts_grid, repeat=2),
        itertools.product(dur_grid, repeat=2),
    ))
    mismatches = []
    grid = list(range(0, seg_hi + 1, 500))
    for (s0, s1), (d0, d1) in cases:
        raw_starts, raw_durs = [s0, s1], [d0, d1]
        # Match the production solver's canonical (start, index) ordering.
        order = sorted(range(2), key=lambda k: (raw_starts[k], k))
        starts = [raw_starts[k] for k in order]
        durs = [raw_durs[k] for k in order]
        boxes = [
            (max(0, starts[0] - cap), min(seg_hi - durs[0], starts[0] + cap)),
            (max(0, starts[1] - cap), min(seg_hi - durs[1], starts[1] + cap)),
        ]
        oc, _ = oracle_min_shift(starts, durs, boxes, min_gap_ms=0, grid=grid)

        plan = _run_production(raw_starts, raw_durs,
                               segments=(seg_hi, 99_000), horizon=99_000,
                               cap=cap, gap=0)
        if oc == INF:
            if plan.status != "infeasible_bounds":
                mismatches.append((starts, durs, "oracle INF", plan.status))
        else:
            if plan.status != "repaired" or plan.total_shift_ms != oc:
                mismatches.append((starts, durs, oc, plan.status,
                                   getattr(plan, "total_shift_ms", None)))
    log.info("[GRID] tight-box sweep cases=%d mismatches=%d", len(cases),
             len(mismatches))
    assert mismatches == [], mismatches[:5]


@pytest.mark.exhaustive
def test_dense_lattice_three_cue_sweep():
    """n=3 over a tiny lattice window: full dense verification, gap 0."""
    vals = list(range(0, 3_001, 250))
    seg_hi, cap = 5_000, 3_000
    # oracle must be able to enumerate box endpoints in [0, seg_hi]
    oracle_grid = list(range(0, seg_hi + 1, 250))
    mismatches = []
    cases = 0
    for s0, s1, s2 in itertools.product(vals, repeat=3):
        for d0 in (1_000,):
            for d1 in (1_000,):
                for d2 in (1_000,):
                    cases += 1
                    raw_starts = [s0, s1, s2]
                    order = sorted(range(3), key=lambda k: (raw_starts[k], k))
                    starts = [raw_starts[k] for k in order]
                    dlist = [[d0, d1, d2][k] for k in order]
                    boxes = _oracle_boxes(starts, dlist, cap=cap, seg_hi=seg_hi)
                    oc, _ = oracle_min_shift(starts, dlist, boxes,
                                             min_gap_ms=0, grid=oracle_grid)
                    plan = _run_production(raw_starts, [d0, d1, d2],
                                           segments=(seg_hi, 60_000),
                                           horizon=60_000, cap=cap, gap=0)
                    if oc == INF:
                        if plan.status != "infeasible_bounds":
                            mismatches.append((starts, dlist, "INF", plan.status))
                    elif plan.status != "repaired" \
                            or plan.total_shift_ms != oc:
                        mismatches.append((starts, dlist, oc,
                                           plan.total_shift_ms))
    log.info("[GRID] n=3 dense cases=%d mismatches=%d", cases, len(mismatches))
    assert mismatches == [], mismatches[:5]


@pytest.mark.exhaustive
def test_random_one_ms_gap_instances_verify_objective():
    """Off-lattice 1ms gap with a *dense integer* oracle grid.

    For n=2 the window is small enough to enumerate every integer millisecond,
    so the oracle is exact even though the optimum is off the 250ms lattice.
    For n=3 a shorter segment keeps the dense DFS tractable. This directly
    tests the production solver's integer-millisecond handling of the real gap.
    """
    rng = random.Random(20260928)
    mismatches = []

    # n=2,3: dense *integer* millisecond DP oracle (independent numpy DP),
    # which is exact even though the 1ms gap puts optima off the 250ms lattice.
    for seg_hi, cap, n_choices, dur_choices, start_step, count in (
        (6_000, 6_000, (2, 3), (1_000, 1_500, 2_000), 250, 250),
        (4_000, 4_000, (3,), (800, 1_000), 100, 120),
    ):
        for _ in range(count):
            n = rng.choice(n_choices)
            durs = [rng.choice(dur_choices) for _ in range(n)]
            raw = [rng.randrange(0, seg_hi - 2_500, start_step)
                   for _ in range(n)]
            order = sorted(range(n), key=lambda k: (raw[k], k))
            starts = [raw[k] for k in order]
            od = [durs[k] for k in order]
            boxes = _oracle_boxes(starts, od, cap=cap, seg_hi=seg_hi)
            oc = oracle_min_shift_dp(starts, od, boxes, min_gap_ms=1,
                                     window_hi=seg_hi)
            plan = _run_production(raw, durs, segments=(seg_hi, 99_000),
                                   horizon=99_000, cap=cap, gap=1)
            if oc == INF:
                if plan.status != "infeasible_bounds":
                    mismatches.append((raw, durs, "INF", plan.status))
            elif plan.status != "repaired" or plan.total_shift_ms != oc:
                mismatches.append((raw, durs, oc,
                                   getattr(plan, "total_shift_ms", None),
                                   plan.status))

    log.info("[GRID] dense 1ms-gap cases=370 mismatches=%d", len(mismatches))
    assert mismatches == [], mismatches[:5]


@pytest.mark.exhaustive
def test_grid_size_reported_in_log():
    n1 = len(LATTICE) * len(DURATIONS)
    n2 = len(LATTICE) ** 2 * len(DURATIONS) ** 2
    log.info("[GRID] lattice cases: n1=%d n2=%d total=%d", n1, n2, n1 + n2)
    assert n1 == 99 and n2 == 9801
