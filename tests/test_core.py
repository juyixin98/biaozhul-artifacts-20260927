"""Core shortest-path tests with HAND-COMPUTED expected values and path audits.

Expected numbers come from the edit model (counting operations), never from
the implementation under test. The unrestricted-vs-restricted distinction is
asserted explicitly so a recurrence swap cannot pass silently.
"""
from __future__ import annotations

import pytest

from app import core
from app.config import CostModel, EPS
from tests.reference_oracle import reference_distance

UNIT = CostModel()


class TestKnownDistances:
    """Classic values under unit costs."""

    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ("", "", 0),
            ("abc", "", 3),
            ("", "abc", 3),
            ("kitten", "sitting", 3),
            ("saturday", "sunday", 3),
            ("teh", "the", 1),
            ("adress", "address", 1),
            ("recieve", "receive", 1),
            ("seperate", "separate", 1),
            # Repeated characters:
            ("banana", "bananas", 1),
            ("banana", "bannana", 1),
            ("aaa", "aaaa", 1),
            ("aaaa", "aa", 2),
            ("aaaa", "aaaaa", 1),
            ("mississippi", "mississippii", 1),
        ],
    )
    def test_known_values(self, a, b, expected):
        assert abs(core.distance(a, b, UNIT) - expected) < EPS
        r = core.distance_with_path(a, b, UNIT)
        assert abs(r.distance - expected) < EPS

    def test_UNRESTRICTED_transposition_chain_ca_to_abc(self):
        # CA -> AC (swap), AC -> ABC (insert B): unrestricted = 2.
        # The restricted OSA recurrence gives 3; asserting 2 locks the model.
        assert core.distance("ca", "abc", UNIT) == 2
        r = core.distance_with_path("ca", "abc", UNIT)
        assert r.distance == 2
        assert core.replay("ca", r.operations) == "abc"
        assert abs(core.recompute_cost(r.operations, UNIT) - 2) < EPS
        kinds = [o.op for o in r.operations]
        assert kinds == ["transpose", "insert"]

    def test_unrestricted_chain_abc_to_bca_is_two_swaps(self):
        # Two adjacent swaps: ABC -> BAC -> BCA. OSA gives 3.
        assert core.distance("abc", "bca", UNIT) == 2
        r = core.distance_with_path("abc", "bca", UNIT)
        assert [o.op for o in r.operations] == ["transpose", "transpose"]
        assert core.replay("abc", r.operations) == "bca"

    def test_repeated_char_swap_chain(self):
        # AAB -> ABA (swap positions 1,2) -> BAA (swap positions 0,1).
        r = core.distance_with_path("aab", "baa", UNIT)
        assert r.distance == 2
        assert [o.op for o in r.operations] == ["transpose", "transpose"]
        assert core.replay("aab", r.operations) == "baa"

    def test_double_transposition_abab(self):
        assert core.distance("abab", "baba", UNIT) == 2
        r = core.distance_with_path("abab", "baba", UNIT)
        assert core.replay("abab", r.operations) == "baba"
        assert all(o.op == "transpose" for o in r.operations)


class TestWeightedSwapChains:
    """The cases a last-occurrence table recurrence gets WRONG."""

    def test_aab_to_baa_two_cheap_swaps(self):
        # swap(ab)=0.2 used twice -> 0.4. A crossed-block table gives 0.9/2.0.
        cm = CostModel(substitute={"a:b": 0.1, "b:a": 0.8},
                       transpose={"a:b": 0.2, "b:a": 1.5})
        r = core.distance_with_path("aab", "baa", cm)
        assert abs(r.distance - 0.4) < EPS
        steps = r.operations
        assert [o.op for o in steps] == ["transpose", "transpose"]
        assert abs(core.recompute_cost(steps, cm) - 0.4) < EPS
        assert core.replay("aab", steps) == "baa"

    def test_abb_to_bba_mirrors_aab(self):
        cm = CostModel(substitute={"a:b": 0.1, "b:a": 0.8},
                       transpose={"a:b": 0.2, "b:a": 1.5})
        # ABB -> BAB -> BBA: each step swaps an "ab" pair, both 0.2 -> 0.4.
        r = core.distance_with_path("abb", "bba", cm)
        assert abs(r.distance - 0.4) < EPS
        assert [o.src for o in r.operations] == ["ab", "ab"]
        assert core.replay("abb", r.operations) == "bba"
        assert abs(r.distance - reference_distance("abb", "bba", cm)) < EPS


class TestAsymmetricCosts:
    def test_asymmetric_substitution_direction(self):
        costs = CostModel(substitute={"i:y": 0.5}, substitute_default=1.0)
        assert abs(core.distance("i", "y", costs) - 0.5) < EPS
        assert abs(core.distance("y", "i", costs) - 1.0) < EPS

    def test_asymmetric_transpose_direction(self):
        one_sided = CostModel(transpose={"e:i": 0.25}, transpose_default=1.0)
        assert abs(core.distance("ei", "ie", one_sided) - 0.25) < EPS
        assert abs(core.distance("ie", "ei", one_sided) - 1.0) < EPS

    def test_expensive_delete_changes_best_script(self):
        costs = CostModel(delete_default=5.0, insert_default=1.0,
                          substitute_default=2.0)
        assert abs(core.distance("ab", "b", costs) - 5.0) < EPS
        assert abs(core.distance("b", "ab", costs) - 1.0) < EPS

    def test_zero_cost_insertion(self):
        costs = CostModel(insert_default=0.0)
        assert core.distance("abc", "abc", costs) == 0
        assert core.distance("abc", "axbyc", costs) == 0


class TestThresholdSearch:
    def test_inf_when_no_script_within_threshold(self):
        r = core.distance_with_path("abc", "xyz", UNIT, threshold=2.0)
        assert r.distance == float("inf")
        assert r.exact is True
        assert r.operations == []

    def test_kept_when_exactly_on_threshold(self):
        r = core.distance_with_path("abc", "abd", UNIT, threshold=1.0)
        assert r.distance == 1.0
        assert core.replay("abc", r.operations) == "abd"

    def test_threshold_search_matches_full_distance(self):
        for a, b in [("kitten", "sitting"), ("saturday", "sunday"),
                     ("recieve", "receive")]:
            full = core.distance(a, b, UNIT)
            bounded = core.distance_with_path(a, b, UNIT, threshold=full)
            assert abs(bounded.distance - full) < EPS


class TestPathAudit:
    @pytest.mark.parametrize("a,b", [
        ("kitten", "sitting"), ("aab", "baa"), ("ca", "abc"),
        ("recieve", "receive"), ("ab", "bxa"),
    ])
    def test_path_replays_and_recosts(self, a, b):
        r = core.distance_with_path(a, b, UNIT)
        assert core.replay(a, r.operations) == b
        assert abs(core.recompute_cost(r.operations, UNIT) - r.distance) < EPS

    def test_replay_detects_bad_precondition(self):
        bad = [core.Operation("transpose", at=0, src="ab", dst="ba"),
               core.Operation("transpose", at=0, src="ab", dst="ba")]
        # second swap no longer sees "ab" at 0 after first applied
        with pytest.raises(ValueError, match="precondition"):
            core.replay("abb", bad)

    def test_operation_dict_is_explicit(self):
        r = core.distance_with_path("teh", "the", UNIT)
        d = r.operations[0].to_dict()
        assert d == {"op": "transpose", "at": 1, "src": "eh", "dst": "he"}
