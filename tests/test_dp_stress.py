"""Stress the two-best DP against brute force on longer inputs.

The two-best DP prunes everything but the two cheapest distinct paths at each
vertex. This is only sound if any global best/second-best path's prefix is
itself best or second-best at that vertex -- these cases use long enough words
that runner-up paths genuinely route through a vertex's second label.
"""
from __future__ import annotations

import random

import pytest

from app.diagnostics import RequestDiagnostics
from app.storage.registry import VersionRegistry
from app.storage.repository import DictionaryRepository

from .oracle import Oracle


@pytest.mark.parametrize("seed", range(12))
def test_longer_inputs_runner_up_routes_through_second_labels(tmp_path, seed):
    rng = random.Random(1000 + seed)
    alphabet = "甲乙丙丁"
    keys: set[str] = set()
    entries: list[dict] = []
    for _ in range(14):
        length = rng.randint(1, 4)
        key = "".join(rng.choice(alphabet) for _ in range(length))
        if key in keys:
            continue
        keys.add(key)
        entries.append({"surface": key, "cost": round(rng.uniform(1.0, 20.0), 4)})

    text = "".join(rng.choice(alphabet) for _ in range(7))

    repo = DictionaryRepository(tmp_path / f"stress-{seed}.db")
    registry = VersionRegistry(repo, unknown_char_cost=25.0)
    registry.publish(entries)
    result = registry.segment(text, RequestDiagnostics(f"stress-{seed}"))

    oracle = Oracle(entries, unknown_char_cost=25.0)
    ranked = oracle.enumerate_paths(text)

    best_surf, best_cost = ranked[0]
    assert tuple(result.best.surfaces) == best_surf
    assert result.best.cost == pytest.approx(best_cost, abs=1e-9)

    if len(ranked) < 2:
        # Genuinely one segmentation possible: service must say so explicitly.
        assert result.runner_up is None
        assert result.gap_status == "unique_path"
        assert result.gap is None
    else:
        second_surf, second_cost = ranked[1]
        assert result.runner_up is not None
        assert result.runner_up.surfaces == tuple(second_surf)
        assert result.runner_up.cost == pytest.approx(second_cost, abs=1e-9)
        assert result.gap == pytest.approx(second_cost - best_cost, abs=1e-9)
        # Non-negative by construction.
        assert result.gap >= 0.0
    assert result.orig_covered and result.reconstructed
