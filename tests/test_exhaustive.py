"""Exhaustive cross-check on short clauses.

For many randomly generated small dictionaries we enumerate **every**
segmentation with the independent oracle and assert the production result has
the globally optimal surface tuple and an exact runner-up gap. Random data is
seeded, so failures are reproducible.
"""
from __future__ import annotations

import random

import pytest

from app.diagnostics import RequestDiagnostics
from app.storage.registry import VersionRegistry
from app.storage.repository import DictionaryRepository

from .oracle import Oracle


def _random_dictionary(rng: random.Random, alphabet: str) -> list[dict]:
    entries: list[dict] = []
    keys: set[str] = set()
    for _ in range(rng.randint(4, 9)):
        length = rng.randint(1, 3)
        key = "".join(rng.choice(alphabet) for _ in range(length))
        if key in keys:
            continue
        keys.add(key)
        # Explicit positive costs keep oracle and service arithmetic identical.
        entries.append({"surface": key, "cost": round(rng.uniform(1.0, 12.0), 4)})
    return entries


@pytest.mark.parametrize("seed", range(30))
def test_exhaustive_small_clauses(tmp_path, seed):
    rng = random.Random(seed)
    alphabet = "abcd"
    entries = _random_dictionary(rng, alphabet)
    text = "".join(rng.choice(alphabet) for _ in range(4))

    repo = DictionaryRepository(tmp_path / f"exhaust-{seed}.db")
    registry = VersionRegistry(repo, unknown_char_cost=15.0)
    registry.publish(entries, note=f"seed-{seed}")
    result = registry.segment(text, RequestDiagnostics(f"ex-{seed}"))

    oracle = Oracle(entries, unknown_char_cost=15.0)
    ranked = oracle.enumerate_paths(text)

    # At least the all-OOV partition always exists.
    assert ranked, "oracle must always find at least one partition"
    best_surf, best_cost = ranked[0]
    assert tuple(result.best.surfaces) == best_surf
    assert result.best.cost == pytest.approx(best_cost, abs=1e-9)

    if len(ranked) > 1:
        second_surf, second_cost = ranked[1]
        assert result.runner_up is not None
        assert result.runner_up.surfaces == second_surf
        assert result.runner_up.cost == pytest.approx(second_cost, abs=1e-9)
        assert result.gap == pytest.approx(second_cost - best_cost, abs=1e-9)
    else:
        assert result.runner_up is None
        assert result.gap_status == "unique_path"

    # Coverage invariant on every random case.
    assert result.orig_covered is True
    assert result.reconstructed is True


def test_exhaustive_enumeration_counts_known_case(registry, diag):
    """The oracle enumerates exactly 2^(n-1) partitions when every prefix and
    single character is a dictionary word."""
    entries = [{"surface": s, "cost": 1.0} for s in
               ("a", "b", "c", "ab", "bc", "abc")]
    registry.publish(entries)
    oracle = Oracle(entries)
    ranked = oracle.enumerate_paths("abc")
    assert len(ranked) == 4  # a|b|c, ab|c, a|bc, abc
    # Every cut is reachable; DP must report the true optimum.
    result = registry.segment("abc", diag)
    assert tuple(result.best.surfaces) == ranked[0][0]
