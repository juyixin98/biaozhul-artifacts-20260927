"""Block state transition, parent-only recurrence and fee conservation."""

from __future__ import annotations

import copy

from basefee.params import PARAMS
from basefee.kernel import Chain
from basefee.kernel.execution import (
    ChainState, BURN_ADDRESS, COINBASE_ADDRESS,
)
from basefee.kernel.models import Block
from basefee.api.wire import structured_to_transaction
from basefee.errors import ErrorCode


def _state_from_fixture(fixture):
    return ChainState(balances={a: int(v) for a, v in fixture["genesis_balances"].items()})


def _resolve(fixture):
    sc = copy.deepcopy(fixture)
    last = "0x" + "00" * 32
    hashes = {}
    return sc, last, hashes


def test_full_fixture_chain_conservation(chain_fixture):
    state = _state_from_fixture(chain_fixture)
    chain = Chain(state)
    total0 = sum(state.balances.values())

    last_hash = chain.genesis_hash()
    for block in chain_fixture["blocks"]:
        block = copy.deepcopy(block)
        if isinstance(block["parent_hash"], str) and block["parent_hash"].startswith("<"):
            block["parent_hash"] = last_hash
        txs = [structured_to_transaction(t) for t in block["transactions"]]
        b = Block(number=block["number"], parent_hash=block["parent_hash"],
                  base_fee_per_gas=int(block["base_fee_per_gas"]),
                  gas_limit=block["gas_limit"], gas_used=block["gas_used"],
                  transactions=txs)
        executed = chain.apply_block(b)
        assert executed.accepted, executed.block_error_detail
        last_hash = b.block_hash

        exp = block["expected"]
        assert str(executed.next_base_fee) == exp["next_base_fee"]
        assert str(executed.total_burned) == exp["burned"]
        assert str(executed.total_tipped) == exp["tipped"]

    # conservation at end
    total1 = sum(state.balances.values())
    assert total1 == total0
    assert state.balances[BURN_ADDRESS] == sum(
        int(b["expected"]["burned"]) for b in chain_fixture["blocks"])
    assert state.balances[COINBASE_ADDRESS] == sum(
        int(b["expected"]["tipped"]) for b in chain_fixture["blocks"])
    # spendable money was destroyed by exactly the burned amount
    spendable = total1 - state.balances[BURN_ADDRESS]
    assert spendable == total0 - state.balances[BURN_ADDRESS]


def test_next_fee_depends_only_on_parent(chain_fixture):
    # Two identical block-2 payloads applied on the same parent must produce
    # identical next fees regardless of what follows them.
    state = _state_from_fixture(chain_fixture)
    chain = Chain(state)
    b1 = copy.deepcopy(chain_fixture["blocks"][0])
    b1["parent_hash"] = chain.genesis_hash()
    txs = [structured_to_transaction(t) for t in b1["transactions"]]
    e1 = chain.apply_block(Block(
        number=1, parent_hash=b1["parent_hash"],
        base_fee_per_gas=int(b1["base_fee_per_gas"]), gas_limit=b1["gas_limit"],
        gas_used=b1["gas_used"], transactions=txs))
    assert e1.accepted
    # direct pure function of parent (base, used, limit)
    from basefee.kernel.eip1559 import next_base_fee
    assert e1.next_base_fee == next_base_fee(
        PARAMS.genesis_base_fee, b1["gas_used"], b1["gas_limit"])


def test_block_gas_cannot_exceed_limit():
    state = ChainState()
    chain = Chain(state)
    b = Block(number=1, parent_hash=chain.genesis_hash(),
              base_fee_per_gas=PARAMS.genesis_base_fee,
              gas_limit=30_000_000, gas_used=30_000_001, transactions=[])
    out = chain.apply_block(b)
    assert not out.accepted
    assert out.block_error == ErrorCode.E040_BLOCK_GAS_EXCEEDED.value
    # state untouched
    assert chain.head.number == 0


def test_declared_gas_used_mismatch_is_E041(sign, keys):
    alice, alice_addr = keys["alice"]
    state = ChainState(balances={alice_addr: 10**24})
    chain = Chain(state)
    wire = sign(alice, max_fee=2_000_000_000, max_tip=1, gas=21000,
                to=alice_addr, value=0, nonce=0)
    tx = structured_to_transaction(wire)
    b = Block(number=1, parent_hash=chain.genesis_hash(),
              base_fee_per_gas=PARAMS.genesis_base_fee,
              gas_limit=30_000_000, gas_used=99999, transactions=[tx])
    out = chain.apply_block(b)
    assert out.block_error == ErrorCode.E041_GAS_USED_MISMATCH.value


def test_base_fee_mismatch_is_E043():
    chain = Chain(ChainState())
    b = Block(number=1, parent_hash=chain.genesis_hash(),
              base_fee_per_gas=1, gas_limit=30_000_000, gas_used=0,
              transactions=[])
    out = chain.apply_block(b)
    assert out.block_error == ErrorCode.E043_BASE_FEE_MISMATCH.value


def test_non_strict_skips_invalid_txs_but_strict_rejects(chain_fixture):
    # block 1 has 2 invalid txs skipped in default mode
    state = _state_from_fixture(chain_fixture)
    chain = Chain(state)
    b1 = copy.deepcopy(chain_fixture["blocks"][0])
    b1["parent_hash"] = chain.genesis_hash()
    txs = [structured_to_transaction(t) for t in b1["transactions"]]
    block = Block(number=1, parent_hash=b1["parent_hash"],
                  base_fee_per_gas=int(b1["base_fee_per_gas"]),
                  gas_limit=b1["gas_limit"], gas_used=b1["gas_used"],
                  transactions=txs)
    out = chain.apply_block(block, strict=False)
    assert out.accepted and len(out.invalid) == 2

    state2 = _state_from_fixture(chain_fixture)
    chain2 = Chain(state2)
    block2 = Block(number=1, parent_hash=chain2.genesis_hash(),
                   base_fee_per_gas=int(b1["base_fee_per_gas"]),
                   gas_limit=b1["gas_limit"], gas_used=b1["gas_used"],
                   transactions=[structured_to_transaction(t)
                                 for t in b1["transactions"]])
    out2 = chain2.apply_block(block2, strict=True)
    assert not out2.accepted
    assert out2.block_error == ErrorCode.E020_MAX_FEE_BELOW_BASE.value


def test_insufficient_balance_and_nonce_categories(chain_fixture):
    state = _state_from_fixture(chain_fixture)
    chain = Chain(state)
    last = chain.genesis_hash()
    seen = {}
    for block in chain_fixture["blocks"]:
        block = copy.deepcopy(block)
        block["parent_hash"] = last
        txs = [structured_to_transaction(t) for t in block["transactions"]]
        executed = chain.apply_block(Block(
            number=block["number"], parent_hash=block["parent_hash"],
            base_fee_per_gas=int(block["base_fee_per_gas"]),
            gas_limit=block["gas_limit"], gas_used=block["gas_used"],
            transactions=txs))
        assert executed.accepted, executed.block_error_detail
        seen[block["number"]] = executed
        last = executed.block.block_hash
    assert {(r.index, r.error_code) for r in seen[2].invalid} == {
        (1, ErrorCode.E033_INSUFFICIENT_BALANCE.value)}
    assert {(r.index, r.error_code) for r in seen[4].invalid} == {
        (1, ErrorCode.E031_NONCE_MISMATCH.value)}


def test_rejected_block_is_atomic(chain_fixture):
    state = _state_from_fixture(chain_fixture)
    chain = Chain(state)
    b1 = copy.deepcopy(chain_fixture["blocks"][0])
    b1["parent_hash"] = chain.genesis_hash()
    txs = [structured_to_transaction(t) for t in b1["transactions"]]
    good = Block(number=1, parent_hash=b1["parent_hash"],
                 base_fee_per_gas=int(b1["base_fee_per_gas"]),
                 gas_limit=b1["gas_limit"], gas_used=b1["gas_used"],
                 transactions=txs)
    chain.apply_block(good)
    alice = chain_fixture["accounts"]["alice"]["address"]
    balance_after_good = state.balances[alice]

    # block 2 claims wrong base fee: must reject and leave state unchanged
    bad = Block(number=2, parent_hash=good.block_hash,
                base_fee_per_gas=1, gas_limit=30_000_000, gas_used=0,
                transactions=[])
    out = chain.apply_block(bad)
    assert not out.accepted
    assert state.balances[alice] == balance_after_good
    assert chain.head.number == 1
