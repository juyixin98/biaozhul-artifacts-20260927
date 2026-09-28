"""Solver tests with hand-computed expected values (not derived from the
solver itself)."""
import pytest

from app.kernel.solver import (
    BUDGET_EXCEEDED, SOLVED, TOO_LARGE, UNSOLVABLE, SolverInputError, solve_repair,
)


def _assert_valid(assignments, min_dur, gap, horizon):
    prev_end = None
    for s, e in assignments:
        assert 0 <= s and e <= horizon
        assert e - s >= min_dur
        if prev_end is not None:
            assert s >= prev_end + gap
        prev_end = e


def test_single_too_short_extended(rlog):
    # [1000,1500] with 1000ms minimum: cheapest fix is 500ms of displacement
    # (extend end to 2000, or pull start to 500 -- both cost 500).
    res = solve_repair([(1000, 1500)], min_duration_ms=1000)
    assert res.status == SOLVED
    assert res.minimal_change_ms == 500
    (s, e), = res.assignments
    assert e - s >= 1000
    rlog("solver_case", case="too_short", input=[[1000, 1500]],
         expected_change=500, actual_change=res.minimal_change_ms,
         verdict="matched hand computation")


def test_negative_duration_rebuilt():
    # [2000,1000]: need duration >= 1000. Writing s=2000-a, e=1000+b the
    # constraint gives a+b >= 2000 and the cost is exactly a+b, so 2000.
    res = solve_repair([(2000, 1000)], min_duration_ms=1000)
    assert res.status == SOLVED
    assert res.minimal_change_ms == 2000
    (s, e), = res.assignments
    assert e - s >= 1000


def test_same_start_pair_exact_assignment():
    # Cues [0,2000] and [0,1500], min duration 1000. Parameterise cue1 end as
    # a (>=1000) and cue2 duration as d (>=1000): cue2 shifts to (a, a+d) and
    # the total cost is (2000-a) + a + |a+d-1500| = 2000 + |a+d-1500|, with
    # a+d >= 2000, so the minimum is 2500 at a=d=1000 -- a unique optimum.
    res = solve_repair([(0, 2000), (0, 1500)], min_duration_ms=1000)
    assert res.status == SOLVED
    assert res.minimal_change_ms == 2500
    assert res.assignments == [(0, 1000), (1000, 2000)]


def test_chained_overlap_minimal():
    # [1000,4000],[3500,6000],[5500,7000]: each 500ms overlap needs >=500ms of
    # displacement on disjoint terms, and 500+500 is achievable by shrinking
    # cue1's end to 3500 and cue2's end to 5500.
    res = solve_repair([(1000, 4000), (3500, 6000), (5500, 7000)],
                       min_duration_ms=1000)
    assert res.status == SOLVED
    assert res.minimal_change_ms == 1000
    _assert_valid(res.assignments, 1000, 0, horizon=7000)


def test_budget_exceeded_not_squeezed(rlog):
    res = solve_repair([(1000, 4000), (3500, 6000), (5500, 7000)],
                       min_duration_ms=1000, budget_ms=500)
    assert res.status == BUDGET_EXCEEDED
    assert res.minimal_change_ms == 1000  # the honest requirement is reported
    assert res.assignments is None        # ...but no over-budget plan is emitted
    rlog("solver_case", case="budget_exceeded", budget=500,
         required=res.minimal_change_ms, verdict="no forced squeeze")


def test_unsolvable_window():
    # 4 cues * 1000ms minimum = 4000ms packed into a 3000ms media window.
    res = solve_repair([(0, 500), (1000, 1500), (2000, 2500), (2500, 2900)],
                       min_duration_ms=1000, media_duration_ms=3000)
    assert res.status == UNSOLVABLE
    assert res.assignments is None
    assert "packed" in res.trace["reason"]


def test_too_large_reported():
    res = solve_repair([(0, 10_000_000)], min_duration_ms=1000, max_grid=100)
    assert res.status == TOO_LARGE
    assert res.trace["grid_points"] > 100


def test_invalid_resolution_rejected():
    with pytest.raises(SolverInputError) as ei:
        solve_repair([(1, 3)], min_duration_ms=1, resolution_ms=10)
    assert ei.value.code == "INVALID_RESOLUTION"


def test_no_cue_ever_dropped():
    cases = [
        ([(0, 2000), (0, 1500)], {}),
        ([(1000, 4000), (3500, 6000), (5500, 7000)], {}),
        ([(2000, 1000)], {}),
        ([(0, 500), (1000, 1500)], {"budget_ms": 10}),
    ]
    for intervals, kw in cases:
        res = solve_repair(intervals, min_duration_ms=1000, **kw)
        if res.assignments is not None:
            assert len(res.assignments) == len(intervals)
