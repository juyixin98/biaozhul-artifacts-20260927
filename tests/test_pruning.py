"""Pruning lower bounds: admissibility and threshold-edge non-loss."""
from __future__ import annotations

import itertools
import random

import pytest

from app import core, indexing
from app.config import CostModel, EPS
from tests.reference_oracle import reference_distance

UNIT = CostModel()


class TestLowerBoundsAdmissible:
    """A lower bound must never exceed the true distance."""

    @pytest.mark.parametrize(
        "a,b",
        list(itertools.product(["", "a", "ab", "abc", "abcd", "abcde"], repeat=2)),
    )
    def test_length_bound_never_above_true(self, a, b):
        lb = indexing.length_lower_bound(len(a), len(b), UNIT)
        assert lb <= core.distance(a, b, UNIT) + EPS

    @pytest.mark.parametrize(
        "a,b",
        list(itertools.product(["a", "ab", "abc", "abcd", "aab", "abab", "xxyyz"], repeat=2)),
    )
    def test_frequency_bound_never_above_true(self, a, b):
        from collections import Counter

        lb = indexing.frequency_lower_bound(Counter(a), Counter(b), UNIT)
        assert lb <= core.distance(a, b, UNIT) + EPS

    def test_bounds_admissible_under_weighted_models(self):
        models = [
            CostModel(insert_default=0.4, delete_default=1.7, substitute_default=0.2,
                      transpose_default=2.3),
            CostModel(insert_default=2.0, delete_default=0.3, substitute_default=0.05,
                      transpose_default=1.1),
            CostModel(substitute_default=0.1, transpose_default=0.2),
            CostModel(insert={"a": 0.01}, delete_default=0.02,
                     substitute_default=0.03, transpose_default=0.04),
        ]
        rng = random.Random(7)
        from collections import Counter

        for model in models:
            for _ in range(200):
                a = "".join(rng.choice("abc") for _ in range(rng.randint(0, 6)))
                b = "".join(rng.choice("abc") for _ in range(rng.randint(0, 6)))
                true = reference_distance(a, b, model)
                lb_len = indexing.length_lower_bound(len(a), len(b), model)
                lb_freq = indexing.frequency_lower_bound(Counter(a), Counter(b), model)
                assert lb_len <= true + 1e-7, (a, b, lb_len, true)
                assert lb_freq <= true + 1e-7, (a, b, lb_freq, true)

    def test_zero_indel_degenerates_length_window(self):
        # Free insertions AND deletions: both sides of the window unbounded.
        costs = CostModel(insert_default=0.0, delete_default=0.0)
        lo, hi = indexing.candidate_bounds("abc", threshold=2.0, costs=costs)
        assert (lo, hi) == (0, 10**9)

    def test_one_sided_free_cost_window(self):
        # Free insertions: longer side unbounded; deletions still bounded.
        costs = CostModel(insert_default=0.0, delete_default=1.0)
        lo, hi = indexing.candidate_bounds("abc", threshold=2.0, costs=costs)
        assert lo == 1 and hi == 10**9
        # Reverse: free deletions, insertions cost 1.
        costs2 = CostModel(insert_default=1.0, delete_default=0.0)
        lo2, hi2 = indexing.candidate_bounds("abc", threshold=2.0, costs=costs2)
        assert lo2 == 0 and hi2 == 5


class TestThresholdEdge:
    def test_candidate_exactly_on_threshold_is_kept(self):
        # distance 1 with threshold exactly 1: both one-shorter and one-longer
        # candidates remain in the window under unit costs.
        lo, hi = indexing.candidate_bounds("abc", 1.0, UNIT)
        assert lo <= 2 and hi >= 4

    def test_float_threshold_boundary(self):
        # Clearly below the integer boundary 2: floor(1.9) = 1.
        lo, hi = indexing.candidate_bounds("abc", 1.9, UNIT)
        assert (lo, hi) == (2, 4)
        # A value within EPS of 2 is treated as 2 (safe over-retrieval;
        # exact A* scoring makes the final threshold decision).
        lo2, hi2 = indexing.candidate_bounds("abc", 1.999999999, UNIT)
        assert (lo2, hi2) == (1, 5)

    def test_weighted_window_uses_directional_costs(self):
        # delete 0.5 (shorter side spans 2), insert 2.0 (longer side spans 0
        # at threshold 1.0, since one insertion alone costs 2).
        costs = CostModel(delete_default=0.5, insert_default=2.0)
        lo, hi = indexing.candidate_bounds("abcd", 1.0, costs)
        assert (lo, hi) == (2, 4)


class TestPruningNoLossAgainstStore:
    """Anything within threshold in the full store survives the filters."""

    def test_retrieve_never_drops_a_qualifying_candidate(self, store, service):
        version_id = store.active_version()
        costs = service.settings.costs
        query = "recieve"
        threshold = 2.0
        survivors, stats = indexing.retrieve_candidates(
            store, version_id, query, threshold, costs,
            sql_limit=service.settings.limits.max_candidates_scored,
        )
        survivor_terms = {row["term"] for row in survivors}

        # Ground truth computed directly from stored data (no SQL window, no
        # frequency bound): the independent exhaustive oracle answers the
        # threshold question for EVERY term. It shares no pruning code with
        # the pipeline (only the cost model).
        all_rows = list(store.iter_candidates(version_id, 0, 10**9, limit=None))
        expected_terms = {
            row["term"]
            for row in all_rows
            if reference_distance(query, row["term"], costs,
                                  threshold=threshold) <= threshold + EPS
        }
        assert expected_terms <= survivor_terms
        assert "receive" in expected_terms and "receive" in survivor_terms

    def test_pruning_reduces_scoring_set(self, store, service):
        version_id = store.active_version()
        costs = service.settings.costs
        all_rows = list(store.iter_candidates(version_id, 0, 10**9, limit=None))
        survivors, stats = indexing.retrieve_candidates(
            store, version_id, "recieve", 1.5, costs,
            sql_limit=5000,
        )
        # The window is strictly smaller than the full dictionary ...
        assert stats.length_window_count < len(all_rows)
        # ... and the frequency bound rejects at least one window row.
        assert stats.bounds_rejected > 0
        assert stats.retrieved == stats.length_window_count
        assert all("term" in s for s in survivors)
