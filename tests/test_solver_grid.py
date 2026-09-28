"""Exhaustive small-grid verification of the repair solver.

The DP solver is checked against an independent brute-force enumerator
(recursive enumeration of every legal placement with cost pruning -- it
shares no code with the solver). For unbounded cases the brute-force horizon
is deliberately larger than the solver's derived bound, so an insufficient
solver bound would show up as a cost mismatch.
"""
import itertools
import random

from app.kernel.solver import SOLVED, UNSOLVABLE, solve_repair


def brute_force_optimal(intervals, min_dur, gap, horizon):
    """Minimum total L1 displacement over all legal placements, or None."""
    best = None
    placements = [(s, e) for s in range(horizon + 1)
                  for e in range(s + min_dur, horizon + 1)]

    def rec(i, prev_end, cost):
        nonlocal best
        if best is not None and cost >= best:
            return
        if i == len(intervals):
            best = cost
            return
        s0, e0 = intervals[i]
        for s, e in placements:
            if s >= prev_end + gap:
                rec(i + 1, e, cost + abs(s - s0) + abs(e - e0))

    rec(0, -(10 ** 9), 0)
    return best


def _check(intervals, min_dur, gap, media):
    res = solve_repair(intervals, min_duration_ms=min_dur, min_gap_ms=gap,
                       media_duration_ms=media, max_grid=1_000_000)
    packed = len(intervals) * min_dur + (len(intervals) - 1) * gap
    horizon = media if media is not None else max(
        max(t for iv in intervals for t in iv), 0) + packed
    brute_horizon = horizon if media is not None else horizon + packed + min_dur + 1
    brute = brute_force_optimal(intervals, min_dur, gap, brute_horizon)
    ctx = f"intervals={intervals} min_dur={min_dur} gap={gap} media={media}"
    if brute is None:
        assert res.status == UNSOLVABLE, (
            f"{ctx}: brute force found no feasible placement but solver said {res.status}")
        return UNSOLVABLE
    assert res.status == SOLVED, (
        f"{ctx}: brute optimum {brute}ms exists but solver said {res.status}")
    assert res.minimal_change_ms == brute, (
        f"{ctx}: solver claims {res.minimal_change_ms}ms, brute force proves {brute}ms")
    prev_end = None
    for s, e in res.assignments:
        assert 0 <= s and e <= horizon and e - s >= min_dur, ctx
        if prev_end is not None:
            assert s >= prev_end + gap, ctx
        prev_end = e
    assert len(res.assignments) == len(intervals), ctx
    return SOLVED


def test_grid_single_cue_exhaustive(rlog):
    checked = 0
    for s0, e0 in itertools.product(range(7), repeat=2):
        for media in (None, 4):
            _check([(s0, e0)], 2, 0, media)
            checked += 1
    rlog("grid_summary", case="n=1 exhaustive grid 0..6, media in {None,4000ms}",
         instances=checked, verdict="solver optimum == brute-force optimum for all")


def test_grid_two_cues_exhaustive(rlog):
    checked = 0
    counts = {SOLVED: 0, UNSOLVABLE: 0}
    for s1, e1, s2, e2 in itertools.product(range(5), repeat=4):
        for media in (None, 5, 4):
            status = _check([(s1, e1), (s2, e2)], 2, 1, media)
            counts[status] += 1
            checked += 1
    rlog("grid_summary", case="n=2 exhaustive grid 0..4, media in {None,5000,4000}",
         instances=checked, solved=counts[SOLVED], unsolvable=counts[UNSOLVABLE],
         verdict="solver optimum == brute-force optimum for all")


def test_grid_three_cues_seeded(rlog):
    rng = random.Random(20260927)
    counts = {SOLVED: 0, UNSOLVABLE: 0}
    for _ in range(150):
        intervals = [(rng.randint(0, 8), rng.randint(0, 8)) for _ in range(3)]
        min_dur = rng.choice([1, 2])
        gap = rng.choice([0, 1])
        media = rng.choice([None, 9, 6])
        status = _check(intervals, min_dur, gap, media)
        counts[status] += 1
    assert counts[SOLVED] > 0 and counts[UNSOLVABLE] > 0
    rlog("grid_summary", case="n=3 seeded(20260927) random, grid 0..8",
         instances=150, solved=counts[SOLVED], unsolvable=counts[UNSOLVABLE],
         verdict="solver optimum == brute-force optimum for all")
