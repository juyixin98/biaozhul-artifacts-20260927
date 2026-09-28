"""Box decomposition tests against exhaustive reference covers."""

from __future__ import annotations

import itertools

import pytest

from reference.naive import naive_box_members, naive_code_members, naive_interval_cover
from zcluster.kernel.coder import DimSpec, MortonCoder
from zcluster.kernel.decompose import decompose_box


def _u(dims, edges):
    out = []
    for d, (lo, hi) in zip(dims, edges):
        out.append((d.to_unsigned(lo), d.to_unsigned(hi)))
    return out


def _assert_covers_all_members(coder, widths, unsigned, budget):
    result = decompose_box(coder, unsigned, budget=budget)
    members = naive_code_members(widths, unsigned)
    covered = [
        any(iv.lo <= c <= iv.hi for iv in result.intervals) for c in members
    ]
    assert covered == [True] * len(members)
    assert all(0 <= iv.lo <= iv.hi < (1 << coder.total_bits)
               for iv in result.intervals)
    return result, members


def test_single_point_box_is_one_interval():
    dims = [DimSpec("a", 2), DimSpec("b", 2)]
    coder = MortonCoder(dims)
    res = decompose_box(coder, [(2, 2), (1, 1)], budget=64)
    assert len(res.intervals) == 1
    assert res.intervals[0].lo == res.intervals[0].hi == coder.encode([2, 1])
    assert res.is_exact


def test_full_domain_is_one_exact_interval():
    dims = [DimSpec("a", 3), DimSpec("b", 2)]
    coder = MortonCoder(dims)
    res = decompose_box(coder, [(0, 7), (0, 3)], budget=1)
    assert len(res.intervals) == 1
    assert res.intervals[0].lo == 0
    assert res.intervals[0].hi == (1 << coder.total_bits) - 1
    assert res.is_exact
    assert res.budget_exhausted is False


def test_known_2x2_box_interval_count_and_runs():
    dims = [DimSpec("a", 2), DimSpec("b", 2)]
    coder = MortonCoder(dims)
    unsigned = [(1, 2), (1, 2)]  # box {(1,1),(1,2),(2,1),(2,2)}
    res = decompose_box(coder, unsigned, budget=64)
    # dim0 occupies even slots: codes for the four points are 3, 9, 6, 12
    members = sorted(naive_code_members([2, 2], unsigned))
    assert members == [3, 6, 9, 12]
    # exact Z decomposition yields runs [3,3], [6,6], [9,9], [12,12]
    assert [(iv.lo, iv.hi) for iv in res.intervals] == \
        [(3, 3), (6, 6), (9, 9), (12, 12)]
    assert res.is_exact


def test_exhaustive_2d_all_boxes_match_naive_cover():
    dims = [DimSpec("a", 3, signed=True), DimSpec("b", 3)]
    coder = MortonCoder(dims)
    widths = [3, 3]
    tested = 0
    for (ax, ay) in itertools.combinations_with_replacement(range(8), 2):
        for (bx, by) in itertools.combinations_with_replacement(range(8), 2):
            unsigned = [(ax, ay), (bx, by)]
            res, members = _assert_covers_all_members(coder, widths, unsigned, 1000)
            # With unlimited budget the kernel's cover is an interval cover;
            # its interval count must never exceed the minimal run-length one.
            minimal = naive_interval_cover(widths, unsigned)
            assert len(res.intervals) <= len(minimal)
            tested += 1
    assert tested == 1296


def test_budget_exhaustion_does_not_miss_and_flags_uncertainty():
    dims = [DimSpec("a", 3), DimSpec("b", 3)]
    coder = MortonCoder(dims)
    unsigned = [(1, 2), (1, 2)]
    res, members = _assert_covers_all_members(coder, [3, 3], unsigned, budget=1)
    assert res.budget_exhausted is True
    assert res.is_exact is False
    # escape is the whole domain: exactly one inexact interval
    assert [(iv.lo, iv.hi, iv.exact) for iv in res.intervals] == \
        [(0, (1 << coder.total_bits) - 1, False)]
    # the inflated candidate set is strictly larger than the box
    assert res.intervals[0].hi - res.intervals[0].lo + 1 > len(members)


def test_thin_boxes_still_covered_under_tight_budgets():
    dims = [DimSpec("a", 4), DimSpec("b", 4), DimSpec("c", 3)]
    coder = MortonCoder(dims)
    thin_boxes = [
        [(5, 5), (0, 15), (2, 2)],       # hyperplane
        [(0, 15), (7, 7), (0, 7)],       # slab
        [(3, 3), (9, 9), (1, 1)],        # single point
    ]
    for unsigned in thin_boxes:
        for budget in (1, 2, 16, 4096):
            res, members = _assert_covers_all_members(
                coder, [4, 4, 3], unsigned, budget
            )
            assert len(members) >= 1


def test_unequal_widths_exhaustive_1d_and_mixed():
    dims = [DimSpec("a", 3), DimSpec("b", 1)]
    coder = MortonCoder(dims)
    for lo in range(8):
        for hi in range(lo, 8):
            for blo in (0, 1):
                for bhi in range(blo, 2):
                    unsigned = [(lo, hi), (blo, bhi)]
                    res, members = _assert_covers_all_members(
                        coder, [3, 1], unsigned, 256
                    )
                    # members are exactly the box points, all distinct codes
                    assert len(members) == (hi - lo + 1) * (bhi - blo + 1)


def test_invalid_budget_and_box_rejected():
    dims = [DimSpec("a", 2)]
    coder = MortonCoder(dims)
    with pytest.raises(ValueError):
        decompose_box(coder, [(0, 3)], budget=0)
    with pytest.raises(ValueError):
        decompose_box(coder, [(2, 1)], budget=8)
    with pytest.raises(ValueError):
        decompose_box(coder, [(0, 4)], budget=8)
