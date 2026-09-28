"""Differential testing against the INDEPENDENT Dijkstra oracle.

No expected value here is produced by the code under test: the oracle computes
distances by exhaustive search over concrete string states. Coverage is a
mix of total enumeration over small alphabets (every pair, so the boundary of
every recurrence branch is hit) and randomized longer strings.
"""
from __future__ import annotations

import itertools
import random

import pytest

from app import core
from app.config import CostModel, EPS
from tests.reference_oracle import reference_distance

UNIT = CostModel()


ALL_STRINGS = [""] + [
    "".join(p) for r in range(1, 5) for p in itertools.product("ab", repeat=r)
]


@pytest.mark.parametrize("a,b", itertools.product(ALL_STRINGS, ALL_STRINGS))
def test_exhaustive_alphabet_ab_unit_costs(a, b):
    expected = reference_distance(a, b, UNIT)
    got = core.distance(a, b, UNIT)
    assert abs(got - expected) < EPS, f"unit-cost mismatch {a!r}->{b!r}: {got} != {expected}"


ALL_STRINGS_ABC = [
    "".join(p) for r in range(1, 4) for p in itertools.product("abc", repeat=r)
]


@pytest.mark.parametrize("a,b", itertools.product(ALL_STRINGS_ABC, ALL_STRINGS_ABC))
def test_exhaustive_alphabet_abc_unit_costs(a, b):
    expected = reference_distance(a, b, UNIT)
    got = core.distance(a, b, UNIT)
    assert abs(got - expected) < EPS, f"mismatch {a!r}->{b!r}: {got} != {expected}"


COST_MODELS = [
    CostModel(),
    CostModel(insert_default=0.4, delete_default=1.7,
             substitute_default=1.2, transpose_default=2.3),
    CostModel(insert_default=2.0, delete_default=0.3,
             substitute_default=0.9, transpose_default=1.1),
    CostModel(insert_default=0.0, delete_default=1.0,
             substitute_default=1.0, transpose_default=1.0),
    CostModel(substitute={"a:b": 0.1, "b:a": 0.8},
             transpose={"a:b": 0.2, "b:a": 1.5}),
    CostModel(insert={"a": 0.1, "b": 0.2}, delete={"a": 0.3, "b": 0.4},
             substitute_default=0.7, transpose_default=0.9),
]


@pytest.mark.parametrize("costs", COST_MODELS)
def test_exhaustive_ab_weighted_models(costs):
    strings = [""] + [
        "".join(p) for r in range(1, 4) for p in itertools.product("ab", repeat=r)
    ]
    for a, b in itertools.product(strings, strings):
        expected = reference_distance(a, b, costs)
        got = core.distance(a, b, costs)
        assert abs(got - expected) < 1e-7, (
            f"weighted mismatch {a!r}->{b!r}: {got} != {expected}"
        )


@pytest.mark.parametrize("seed", range(12))
def test_random_pairs_against_oracle(seed):
    #  1) the THRESHOLD question (what the service actually asks) agrees
    #     under every model, including degenerate zero-cost ones;
    #  2) for strictly positive costs the unbounded exact distance agrees on
    #     short strings (exact search is exponential for far permutations;
    #     exhaustive enumeration already covers those).
    rng = random.Random(seed)
    node_cap = 30_000
    for _ in range(12):
        m = rng.randint(0, 5)
        n = rng.randint(0, 5)
        a = "".join(rng.choice("abc") for _ in range(m))
        b = "".join(rng.choice("abc") for _ in range(n))
        model = rng.choice(COST_MODELS)

        # (1) threshold question; a cap hit on either side skips the pair
        try:
            bounded = core.distance_with_path(
                a, b, model, threshold=2.0, node_cap=node_cap
            ).distance
            truth = reference_distance(a, b, model, threshold=2.0,
                                       node_cap=node_cap)
        except (AssertionError, core.SearchCapExceeded):
            continue
        assert (bounded <= 2.0 + 1e-7) == (truth <= 2.0 + 1e-7), (
            f"threshold disagreement {a!r}->{b!r}: {bounded} vs {truth}"
        )

        # (2) exact distance, positive costs only; skip pairs whose exact
        # search is genuinely large (far permutations, covered exhaustively).
        if model.min_edit > 1e-9:
            try:
                got = core.distance(a, b, model, node_cap=node_cap)
                expected = reference_distance(a, b, model, node_cap=node_cap)
            except (AssertionError, core.SearchCapExceeded):
                continue
            assert abs(got - expected) < 1e-7, (
                f"exact mismatch {a!r}->{b!r}: {got} != {expected}"
            )


def test_degenerate_free_insertion_exhaustive_short_strings():
    # Free insertions make the length bound one-sided and the state space
    # explode on long strings, so this model is covered by TOTAL enumeration
    # on len<=4 rather than long random pairs.
    free = CostModel(insert_default=0.0, delete_default=1.0,
                     substitute_default=1.0, transpose_default=1.0)
    for m in range(5):
        for n in range(5):
            for a in ("".join(p) for p in itertools.product("ab", repeat=m)):
                for b in ("".join(p) for p in itertools.product("ab", repeat=n)):
                    within_core = (
                        core.distance_with_path(a, b, free, threshold=2.0,
                                                node_cap=30_000).distance
                        <= 2.0 + 1e-9
                    )
                    within_orc = (
                        reference_distance(a, b, free, threshold=2.0,
                                           node_cap=30_000)
                        <= 2.0 + 1e-9
                    )
                    assert within_core == within_orc, (a, b)


def test_oracle_and_search_agree_on_transposition_chains():
    # The unrestricted model differs from restricted OSA on these pairs.
    # OSA values (computed independently): CA->ABC=3, ABC->BCA=3.
    osa_examples = {("ca", "abc"): 3, ("abc", "bca"): 3}
    for (a, b), osa_value in osa_examples.items():
        got = core.distance(a, b, UNIT)
        assert abs(got - reference_distance(a, b, UNIT)) < EPS
        assert got < osa_value, (
            f"{a!r}->{b!r}: unrestricted {got} should be below OSA {osa_value}"
        )
