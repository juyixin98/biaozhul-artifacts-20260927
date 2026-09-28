"""Hand-computed base-fee recurrence vectors (asserts concrete numbers).

Expected values come from fixtures/hand_vectors.json which contains arithmetic
scratch and was derived independently of the kernel.
"""

from __future__ import annotations

from basefee.kernel.eip1559 import compute_next_base_fee_step, next_base_fee
from basefee.params import PARAMS


def test_all_hand_base_fee_vectors(hand_vectors):
    for v in hand_vectors["base_fee_vectors"]:
        step = compute_next_base_fee_step(v["parent_base_fee"], v["gas_used"],
                                          v["gas_limit"])
        assert step.next_base_fee == v["expected_next_base_fee"], v["name"]
        assert step.direction == v["expected_direction"], v["name"]
        assert step.delta_numerator == v["expected_delta_numerator"], v["name"]
        if "expected_delta_after_first_floor" in v:
            assert step.delta_after_first_floor == v[
                "expected_delta_after_first_floor"], v["name"]
        assert step.delta_final == v["expected_delta_final"], v["name"]
        assert step.target_gas == v["gas_limit"] // PARAMS.elasticity_multiplier
        if v.get("applied_min_increment"):
            assert step.applied_min_increment is True


def test_six_block_walk(hand_vectors):
    v = hand_vectors["multi_block_vectors"][0]
    base = 1_000_000_000
    out = []
    for used in v["gas_used_sequence"]:
        base = next_base_fee(base, used, v["gas_limit"])
        out.append(base)
    assert out == v["expected_base_fees_after"]


def test_gas_used_above_limit_rejected():
    import pytest
    with pytest.raises(ValueError):
        next_base_fee(1_000, gas_used=30_000_001, gas_limit=30_000_000)


def test_odd_gas_limit_rejected():
    import pytest
    with pytest.raises(ValueError):
        next_base_fee(1_000, gas_used=5, gas_limit=21)


def test_full_then_empty_is_asymmetric():
    # Documented EIP-1559 behavior: full then empty does not return to origin.
    up = next_base_fee(1_000_000_000, 30_000_000, 30_000_000)
    back = next_base_fee(up, 0, 30_000_000)
    assert up == 1_125_000_000
    assert back == 984_375_000
    assert back != 1_000_000_000


def test_floor_division_direction_is_downward():
    # 30//8 = 3 under floor; any rounding-up convention gives 4.
    assert next_base_fee(100, 13, 20) == 103
    assert next_base_fee(1, 9, 20) == 1
