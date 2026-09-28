"""Fee-cap / effective-price / overflow validation against hand vectors."""

import os
import sys

import pytest

from basefee_model.config import MAX_U256
from basefee_model.core.validation import intrinsic_gas, validate_fee_caps
from basefee_model.encoding.transaction import Transaction
from basefee_model.errors import FailureCode, TransactionError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "oracle"))
from reference_oracle import oracle_effective_price  # noqa: E402


def _tx2(**kw):
    kw.setdefault("type", 2)
    kw.setdefault("nonce", 0)
    kw.setdefault("gas_limit", 21_000)
    kw.setdefault("to", b"\x11" * 20)
    kw.setdefault("value", 0)
    kw.setdefault("chain_id", 1559)
    kw.setdefault("max_fee_per_gas", 0)
    kw.setdefault("max_priority_fee_per_gas", 0)
    return Transaction(**kw)


def _legacy(**kw):
    kw.setdefault("type", 0)
    kw.setdefault("nonce", 0)
    kw.setdefault("gas_limit", 21_000)
    kw.setdefault("to", b"\x11" * 20)
    kw.setdefault("value", 0)
    kw.setdefault("chain_id", 1559)
    kw.setdefault("gas_price", 0)
    return Transaction(**kw)


@pytest.mark.hand
def test_effective_gas_price_hand_vectors(hand_vectors):
    for case in hand_vectors["effective_gas_price"]:
        if case["tx_type"] == 2:
            tx = _tx2(max_fee_per_gas=case["max_fee"],
                      max_priority_fee_per_gas=case["priority"])
        else:
            tx = _legacy(gas_price=case["gas_price"])
        eff = tx.effective_gas_price(case["base_fee"])
        tip = tx.priority_fee_per_gas(case["base_fee"])
        o_eff, o_tip = oracle_effective_price(
            case["tx_type"], case["base_fee"], max_fee=case["max_fee"] or 0,
            priority=case["priority"] or 0, gas_price=case["gas_price"] or 0)
        assert eff == case["effective"] == o_eff, case["name"]
        assert tip == case["tip"] == o_tip, case["name"]
        assert eff - tip == case["burn_per_gas"] == case["base_fee"], case["name"]


@pytest.mark.hand
def test_intrinsic_gas_hand_vectors(hand_vectors):
    for case in hand_vectors["intrinsic_gas"]:
        data = bytes.fromhex(case["data_hex"][2:])
        tx = _tx2(gas_limit=1_000_000, data=data)
        assert intrinsic_gas(tx) == case["expected"], case["name"]


def test_intrinsic_gas_rejects_gas_limit_below_floor():
    tx = _tx2(gas_limit=21_000, data=b"\x00\x00")  # needs 21008
    from basefee_model.core.validation import validate_intrinsic_gas
    with pytest.raises(TransactionError) as exc2:
        validate_intrinsic_gas(tx)
    assert exc2.value.code == FailureCode.GAS_LIMIT_EXCEEDED_INTRINSIC


@pytest.mark.hand
def test_invalid_fee_caps_categorized(hand_vectors):
    for case in hand_vectors["invalid_fee_caps"]:
        if case["tx_type"] == 2:
            tx = _tx2(max_fee_per_gas=case["max_fee"],
                      max_priority_fee_per_gas=case["priority"],
                      gas_limit=case.get("gas_limit", 21_000))
        else:
            tx = _legacy(gas_price=case["gas_price"],
                         gas_limit=case.get("gas_limit", 21_000))
        with pytest.raises(TransactionError) as exc:
            validate_fee_caps(tx, case["base_fee"])
        assert exc.value.code.value == case["expected_code"], (
            f"{case['name']}: got {exc.value.code.value}, "
            f"want {case['expected_code']}")


def test_cap_equals_priority_boundary_accepted():
    tx = _tx2(max_fee_per_gas=100, max_priority_fee_per_gas=100)
    validate_fee_caps(tx, 50)  # must not raise
    assert tx.effective_gas_price(50) == 100


def test_legacy_zero_base_fee_accepts_low_price():
    tx = _legacy(gas_price=1)
    validate_fee_caps(tx, 0)  # valid when base fee is zero


def test_overflow_constants_are_u256():
    assert MAX_U256 == 2 ** 256 - 1
    tx = _tx2(max_fee_per_gas=MAX_U256 + 1, max_priority_fee_per_gas=1)
    with pytest.raises(TransactionError) as exc:
        validate_fee_caps(tx, 0)
    assert exc.value.code == FailureCode.FEE_CAP_OVERFLOWS_U256
