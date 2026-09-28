"""Transaction validity: concrete fee results AND named failure categories."""

from __future__ import annotations

from basefee.kernel.eip1559 import effective_priority_tip, effective_gas_price
from basefee.params import UINT256_MAX
from basefee.kernel.execution import validate_static, recover_sender
from basefee.kernel.models import Transaction, Signature
from basefee.errors import ErrorCode


def test_hand_transaction_pricing_vectors(hand_vectors):
    for v in hand_vectors["transaction_vectors"]:
        if v.get("expected_valid") is False:
            continue
        tip = effective_priority_tip(
            base_fee=v["base_fee"], max_fee_per_gas=v["max_fee_per_gas"],
            max_priority_fee_per_gas=v["max_priority_fee_per_gas"])
        assert tip == v["expected_effective_tip"], v["name"]
        price = effective_gas_price(
            base_fee=v["base_fee"], max_fee_per_gas=v["max_fee_per_gas"],
            max_priority_fee_per_gas=v["max_priority_fee_per_gas"])
        assert price == v["expected_effective_price"], v["name"]
        assert v["base_fee"] * v["gas"] == v["expected_burned"], v["name"]
        assert tip * v["gas"] == v["expected_tip_total"], v["name"]
        if "expected_sender_debit" in v:
            assert (v["base_fee"] * v["gas"] + tip * v["gas"]
                    == v["expected_sender_debit"]), v["name"]


def _tx(**kw):
    base = dict(chain_id=15590, nonce=0, max_fee_per_gas=2_000_000_000,
                max_priority_fee_per_gas=100_000_000, gas_limit=21000,
                to="0x" + "aa" * 20, value=0, data=b"",
                signature=Signature(r=1, s=1, v=0))
    base.update(kw)
    return Transaction(**base)


def test_fee_cap_below_base_is_E020():
    err = validate_static(_tx(max_fee_per_gas=999_999_999), base_fee=1_000_000_000)
    assert err == ErrorCode.E020_MAX_FEE_BELOW_BASE.value


def test_gas_below_intrinsic_is_E030():
    err = validate_static(_tx(gas_limit=20999), base_fee=1)
    assert err == ErrorCode.E030_GAS_LIMIT_TOO_LOW.value


def test_overflow_is_E032():
    err = validate_static(
        _tx(max_fee_per_gas=UINT256_MAX, gas_limit=21000), base_fee=1)
    assert err == ErrorCode.E032_FEE_OVERFLOW.value


def test_tip_cap_above_fee_cap_is_valid():
    # Per EIP-1559 this is legal; tip is auto-capped to the slack.
    err = validate_static(
        _tx(max_fee_per_gas=1_200_000_000, max_priority_fee_per_gas=5_000_000_000),
        base_fee=1_000_000_000)
    assert err is None
    assert effective_priority_tip(
        base_fee=1_000_000_000, max_fee_per_gas=1_200_000_000,
        max_priority_fee_per_gas=5_000_000_000) == 200_000_000


def test_wrong_chain_id_is_E004():
    err = validate_static(_tx(chain_id=1), base_fee=1)
    assert err == ErrorCode.E004_BAD_CHAIN_ID.value


def test_bad_recovery_index_is_E010():
    tx = _tx(signature=Signature(r=1, s=1, v=2))
    sender, err = recover_sender(tx)
    assert err == ErrorCode.E010_SIGNATURE_MALFORMED.value
    assert sender is None


def test_high_s_is_E012():
    from basefee.encoding.crypto import CURVE
    tx = _tx(signature=Signature(r=1, s=CURVE.order - 1, v=0))
    sender, err = recover_sender(tx)
    assert err == ErrorCode.E012_SIGNATURE_HIGH_S.value


def test_unsigned_tx_is_E010():
    tx = Transaction(chain_id=15590, nonce=0, max_fee_per_gas=1,
                     max_priority_fee_per_gas=1, gas_limit=21000,
                     to="0x" + "aa" * 20, value=0)
    sender, err = recover_sender(tx)
    assert err == ErrorCode.E010_SIGNATURE_MALFORMED.value
