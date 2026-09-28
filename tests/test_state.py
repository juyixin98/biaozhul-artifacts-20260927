"""Chain-state kernel: block validity and fee conservation."""

import pytest

from basefee_model.core.state import ChainState
from basefee_model.errors import BlockError, FailureCode, TransactionError
from basefee_model.config import DEFAULT_GAS_LIMIT
from basefee_model.fixtures import (sign_eip1559_tx, sign_legacy_tx, wallet)

GL = DEFAULT_GAS_LIMIT
T = GL // 2


def _chain(funder, balance=10 ** 30, base=1_000_000_000, gas_used=None):
    chain = ChainState(genesis_base_fee=base, gas_limit=GL,
                       genesis_gas_used=gas_used if gas_used is not None else T)
    chain.add_account(funder.address, balance=balance, nonce=0)
    return chain


def test_target_load_keeps_base_fee(funder):
    chain = _chain(funder)
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=10 ** 9, gas_limit=T)
    blk = chain.build_block([raw], tx_gas_used=[T])
    res = chain.apply_block(blk)
    assert res.base_fee == 1_000_000_000
    assert res.next_base_fee == 1_000_000_000


def test_block_gas_over_limit_rejected(funder):
    chain = _chain(funder)
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=1, gas_limit=GL + 1)
    blk = chain.build_block([raw], tx_gas_used=[GL + 1])
    with pytest.raises(BlockError) as exc:
        chain.apply_block(blk)
    assert exc.value.code == FailureCode.BLOCK_GAS_OVER_LIMIT


def test_wrong_base_fee_header_rejected(funder):
    chain = _chain(funder)
    # Genesis balanced -> block1 must be 1e9; force a wrong header.
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=1, gas_limit=21_000)
    blk = chain.build_block([raw], tx_gas_used=[21_000])
    blk.base_fee = 999_999_999  # tamper
    with pytest.raises(BlockError) as exc:
        chain.apply_block(blk)
    assert exc.value.code == FailureCode.BAD_BASE_FEE


def test_nonmatching_parent_hash_rejected(funder):
    chain = _chain(funder)
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=1, gas_limit=21_000)
    blk = chain.build_block([raw], tx_gas_used=[21_000])
    blk.parent_hash = b"\xab" * 32
    blk.hash = blk.compute_hash()
    with pytest.raises(BlockError) as exc:
        chain.apply_block(blk)
    assert exc.value.code == FailureCode.BAD_PARENT


def test_nonce_too_low_and_too_high(funder):
    chain = _chain(funder)
    # First tx nonce 1 -> too high (gap)
    _, raw = sign_eip1559_tx(funder, 1, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=1, gas_limit=21_000)
    blk = chain.build_block([raw], tx_gas_used=[21_000])
    with pytest.raises(TransactionError) as exc:
        chain.apply_block(blk)
    assert exc.value.code == FailureCode.NONCE_TOO_HIGH

    # valid nonce 0
    _, raw0 = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                              max_priority_fee_per_gas=1, gas_limit=21_000)
    blk0 = chain.build_block([raw0], tx_gas_used=[21_000])
    chain.apply_block(blk0)
    # reuse nonce 0 -> too low
    _, raw_again = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                                   max_priority_fee_per_gas=1, gas_limit=21_000)
    blk1 = chain.build_block([raw_again], tx_gas_used=[21_000])
    with pytest.raises(TransactionError) as exc:
        chain.apply_block(blk1)
    assert exc.value.code == FailureCode.NONCE_TOO_LOW


def test_insufficient_funds_rejected(funder):
    chain = _chain(funder, balance=100)
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 12,
                             max_priority_fee_per_gas=1, gas_limit=21_000)
    blk = chain.build_block([raw], tx_gas_used=[21_000])
    with pytest.raises(TransactionError) as exc:
        chain.apply_block(blk)
    assert exc.value.code == FailureCode.INSUFFICIENT_FUNDS


def test_unknown_sender_rejected(funder):
    chain = ChainState(genesis_base_fee=1_000_000_000, gas_limit=GL,
                       genesis_gas_used=T)  # no accounts funded
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=1, gas_limit=21_000)
    blk = chain.build_block([raw], tx_gas_used=[21_000])
    with pytest.raises(TransactionError) as exc:
        chain.apply_block(blk)
    assert exc.value.code == FailureCode.INSUFFICIENT_FUNDS


def test_fee_conservation_hand_vector(funder, hand_vectors):
    cv = hand_vectors["fee_conservation"]
    chain = _chain(funder, balance=10 ** 24, base=cv["base_fee"])
    receiver = wallet("rcpt_cons")
    _, raw = sign_eip1559_tx(
        funder, 0, max_fee_per_gas=cv["max_fee"],
        max_priority_fee_per_gas=cv["priority"], gas_limit=cv["gas_used"],
        value=cv["value"], to=receiver.address)
    blk = chain.build_block([raw], tx_gas_used=[cv["gas_used"]])
    res = chain.apply_block(blk)
    rcpt = res.receipts[0]
    assert rcpt.effective_gas_price == cv["effective_gas_price"]
    assert rcpt.burned == cv["burned"]
    assert rcpt.tip_charged == cv["tips"]
    assert rcpt.sender_debit == cv["sender_debit"]
    assert rcpt.sender_debit == rcpt.burned + rcpt.tip_charged + rcpt.value
    # recipient received the value
    assert chain.get_account(receiver.address).balance == cv["value"]
    assert chain.conservation_report()["conserved"] is True


def test_multi_block_chain_conservation(funder):
    chain = _chain(funder, balance=10 ** 30)
    nonce = 0
    for used in (T, GL, GL, 0, 0, T):
        if used == 0:
            blk = chain.build_block([], tx_gas_used=[])
        else:
            _, raw = sign_eip1559_tx(funder, nonce, max_fee_per_gas=10 ** 18,
                                     max_priority_fee_per_gas=10 ** 9,
                                     gas_limit=max(used, 21_000))
            blk = chain.build_block([raw], tx_gas_used=[used])
            nonce += 1
        chain.apply_block(blk)
    rep = chain.conservation_report()
    assert rep["conserved"] is True
    assert rep["difference"] == 0


def test_block_number_must_be_sequential(funder):
    chain = _chain(funder)
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=1, gas_limit=21_000)
    blk = chain.build_block([raw], tx_gas_used=[21_000])
    blk.number = 5
    blk.hash = blk.compute_hash()
    with pytest.raises(BlockError) as exc:
        chain.apply_block(blk)
    assert exc.value.code == FailureCode.BAD_BLOCK_NUMBER


def test_chain_id_mismatch_rejected(funder):
    # Chain enforces chain_id 1559; a tx signed for chain 1 must be rejected.
    chain = ChainState(genesis_base_fee=1_000_000_000, gas_limit=GL,
                       genesis_gas_used=T, chain_id=1559)
    chain.add_account(funder.address, balance=10 ** 30, nonce=0)
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=1, gas_limit=21_000,
                             chain_id=1)
    blk = chain.build_block([raw], tx_gas_used=[21_000])
    with pytest.raises(TransactionError) as exc:
        chain.apply_block(blk)
    assert exc.value.code == FailureCode.CHAIN_ID_MISMATCH


def test_chain_id_match_accepted(funder):
    chain = ChainState(genesis_base_fee=1_000_000_000, gas_limit=GL,
                       genesis_gas_used=T, chain_id=1559)
    chain.add_account(funder.address, balance=10 ** 30, nonce=0)
    _, raw = sign_eip1559_tx(funder, 0, max_fee_per_gas=10 ** 18,
                             max_priority_fee_per_gas=1, gas_limit=21_000,
                             chain_id=1559)
    blk = chain.build_block([raw], tx_gas_used=[21_000])
    res = chain.apply_block(blk)
    assert res.base_fee == 1_000_000_000
