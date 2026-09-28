"""SQLite index storage tests: persistence, big-int handling, duplicate/gap rules."""

from __future__ import annotations

import copy

import pytest

from basefee.kernel import Chain
from basefee.kernel.execution import ChainState
from basefee.kernel.models import Block
from basefee.storage import Store, StorageError
from basefee.params import UINT256_MAX
from basefee.errors import ErrorCode
from basefee.api.wire import structured_to_transaction


def test_persist_and_reload(tmp_path, chain_fixture):
    db = tmp_path / "chain.db"
    state = ChainState(
        balances={a: int(v) for a, v in chain_fixture["genesis_balances"].items()})
    chain = Chain(state)
    store = Store(str(db))
    last = chain.genesis_hash()
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
        store.save_executed(executed)
        last = executed.block.block_hash
    store.close()

    store2 = Store(str(db))
    assert store2.head_number() == 4
    row = store2.get_block_row(2)
    assert row is not None
    assert int(row["base_fee"]) > 0
    # full block
    assert int(row["gas_used"]) == 30_000_000
    invalid = store2.get_invalid(1)
    codes = {r["error_code"] for r in invalid}
    assert "E020_MAX_FEE_BELOW_BASE" in codes and "E011_SIGNATURE_INVALID" in codes
    store2.close()


def test_duplicate_block_number_rejected(chain_fixture):
    state = ChainState(
        balances={a: int(v) for a, v in chain_fixture["genesis_balances"].items()})
    chain = Chain(state)
    store = Store(":memory:")
    b1 = copy.deepcopy(chain_fixture["blocks"][0])
    b1["parent_hash"] = chain.genesis_hash()
    executed = chain.apply_block(Block(
        number=1, parent_hash=b1["parent_hash"],
        base_fee_per_gas=int(b1["base_fee_per_gas"]), gas_limit=b1["gas_limit"],
        gas_used=b1["gas_used"],
        transactions=[structured_to_transaction(t) for t in b1["transactions"]]))
    store.save_executed(executed)
    with pytest.raises(StorageError) as exc:
        store.save_executed(executed)
    assert exc.value.code == ErrorCode.E050_BLOCK_EXISTS.value


def test_gap_in_block_numbers_rejected(chain_fixture):
    state = ChainState(
        balances={a: int(v) for a, v in chain_fixture["genesis_balances"].items()})
    chain = Chain(state)
    # advance kernel to block 1 but give the store a "fresh" head expecting 0
    b1 = copy.deepcopy(chain_fixture["blocks"][0])
    b1["parent_hash"] = chain.genesis_hash()
    executed = chain.apply_block(Block(
        number=1, parent_hash=b1["parent_hash"],
        base_fee_per_gas=int(b1["base_fee_per_gas"]), gas_limit=b1["gas_limit"],
        gas_used=b1["gas_used"],
        transactions=[structured_to_transaction(t) for t in b1["transactions"]]))
    store = Store(":memory:")
    # pretend next number is 3
    executed.block.number = 3
    with pytest.raises(StorageError) as exc:
        store.save_executed(executed)
    assert exc.value.code == ErrorCode.E052_REPLAY_GAP.value


def test_uint256_decimal_text_roundtrip():
    store = Store(":memory:")
    assert store.get_meta("protocol_version")
    # exercise int->text->int via balances table indirectly
    v = UINT256_MAX
    store.conn.execute(
        "INSERT INTO balances(address, balance) VALUES(?, ?) "
        "ON CONFLICT(address) DO UPDATE SET balance=excluded.balance",
        ("0x" + "ab" * 20, str(v)))
    assert store.get_balance("0x" + "ab" * 20) == v
