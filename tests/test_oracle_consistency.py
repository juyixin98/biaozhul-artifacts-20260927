"""Kernel results must agree with the independent oracle over fuzzed inputs."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference import oracle as O
from basefee.kernel.eip1559 import compute_next_base_fee_step, effective_priority_tip


@pytest.mark.parametrize("seed", range(20))
def test_recurrence_matches_oracle_random(seed):
    rng = random.Random(seed)
    for _ in range(60):
        limit = rng.choice([20, 22, 100, 1000, 1_000_000, 30_000_000])
        base = rng.choice([0, 1, 2, 3, 7, 100, 999_999, 10**9, 2**64, 2**200])
        used = rng.randint(0, limit)
        k = compute_next_base_fee_step(base, used, limit)
        o = O.oracle_next_base_fee(base, used, limit)
        assert k.next_base_fee == o
        assert k.direction == ("up" if used > limit // 2 else
                               "down" if used < limit // 2 else "flat")


@pytest.mark.parametrize("seed", range(10))
def test_effective_tip_matches_oracle(seed):
    rng = random.Random(100 + seed)
    for _ in range(200):
        base = rng.randint(0, 10**15)
        max_fee = rng.randint(base, base + 10**15)
        max_tip = rng.randint(0, 10**15)
        k = effective_priority_tip(base_fee=base, max_fee_per_gas=max_fee,
                                   max_priority_fee_per_gas=max_tip)
        assert k == O.oracle_effective_tip(base, max_fee, max_tip)


def test_oracle_independence_guard():
    # The oracle module must not import the production package.
    src = (ROOT / "reference" / "oracle.py").read_text()
    assert "from basefee" not in src
    assert "import basefee" not in src
