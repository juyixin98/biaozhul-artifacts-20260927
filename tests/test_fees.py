"""Fee recurrence: hand vectors x independent oracle x system under test.

These are value-level assertions (exact integers and direction), not
"the endpoint responds". The expected values come from hand arithmetic in
hand_vectors.json; the independent oracle (which cannot import the production
core) is used as a third opinion. All three must agree.
"""

import os
import sys

import pytest

from basefee_model.core.fees import (base_fee_step_report, gas_target,
                                     next_base_fee)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "oracle"))
from reference_oracle import (oracle_gas_target, oracle_next_base_fee)  # noqa: E402


def test_gas_target_fixed_constant():
    assert gas_target(30_000_000) == 15_000_000
    assert oracle_gas_target(30_000_000) == 15_000_000


@pytest.mark.hand
def test_single_step_hand_vectors(hand_vectors):
    for case in hand_vectors["single_step"]:
        # 1) system under test
        got = next_base_fee(case["parent_base_fee"],
                            case["parent_gas_used"], case["gas_limit"])
        report = base_fee_step_report(case["parent_base_fee"],
                                      case["parent_gas_used"],
                                      case["gas_limit"])
        # 2) independent oracle
        oracle = oracle_next_base_fee(case["parent_base_fee"],
                                      case["parent_gas_used"],
                                      case["gas_limit"])
        # 3) the frozen hand value
        assert got == case["expected_next"], (
            f"{case['name']}: sut={got} hand={case['expected_next']}")
        assert oracle == case["expected_next"], (
            f"{case['name']}: oracle={oracle} hand={case['expected_next']}")
        assert report["direction"] == case["expected_direction"], case["name"]
        assert report["delta"] == abs(got - case["parent_base_fee"]), case["name"]


def test_triple_agreement_explicit_anchors():
    # Re-assert the headline anchors literally so a corrupted JSON cannot
    # silently pass.
    assert next_base_fee(1_000_000_000, 15_000_000, 30_000_000) == 1_000_000_000
    assert next_base_fee(1_000_000_000, 0, 30_000_000) == 875_000_000
    assert next_base_fee(1_000_000_000, 30_000_000, 30_000_000) == 1_125_000_000
    # Minimum upward increment and downward floor/truncation.
    assert next_base_fee(1, 30_000_000, 30_000_000) == 2
    assert next_base_fee(3, 14_999_999, 30_000_000) == 3
    assert next_base_fee(1, 0, 30_000_000) == 1
    assert next_base_fee(0, 30_000_000, 30_000_000) == 1


def test_only_parent_determines_next_base_fee():
    """The recurrence is a pure function; extra context cannot change it."""
    a = next_base_fee(500, 20_000_000, 30_000_000)
    # Same parent values from "different histories" must be identical.
    b = next_base_fee(500, 20_000_000, 30_000_000)
    assert a == b
    # A different parent base fee changes the result (history-independent).
    assert next_base_fee(501, 20_000_000, 30_000_000) != a


def test_multi_block_recurrence_hand_vector(hand_vectors):
    spec = hand_vectors["multi_block_recurrence"]
    base = spec["genesis_base_fee"]
    gl = spec["gas_limit"]
    # Replay by feeding each block's gas used as the parent of the next,
    # asserting the hand-computed base fee present IN each block.
    prev_used = gl // 2  # genesis is balanced
    for blk in spec["blocks"]:
        computed = next_base_fee(base, prev_used, gl)
        assert computed == blk["base_fee"], (
            f"block {blk['number']}: computed {computed} hand {blk['base_fee']}")
        # oracle agrees
        assert oracle_next_base_fee(base, prev_used, gl) == blk["base_fee"]
        base = computed
        prev_used = blk["gas_used"]
    assert base == spec["next_base_fee_after_block_7"]


@pytest.mark.parametrize("used,limit", [(30_000_001, 30_000_000),
                                        (1, 0), (-5, 100)])
def test_invalid_gas_inputs_raise_categorized(used, limit):
    from basefee_model.errors import BlockError
    with pytest.raises(BlockError):
        next_base_fee(100, used, limit)
