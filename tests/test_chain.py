"""候选区块顺序、确认落账、丢弃、回滚测试。"""

from __future__ import annotations

import pytest

from localtxpool.errors import (
    BlockConflict, BlockNotProposed, EmptyBlock, RollbackTooDeep,
)
from tests.conftest import addr_for


def _hashes(service, txs):
    return ["0x" + t.hash().hex() for t in txs]


def test_candidate_order_is_price_desc_with_nonce_constraint(service, fund, sign, submit):
    fund("alice", 10**20); fund("bob", 10**20)
    a0 = sign("alice", nonce=0, gas_price=10)
    a1 = sign("alice", nonce=1, gas_price=90)   # 高价但受 nonce0 约束
    b0 = sign("bob", nonce=0, gas_price=40)
    submit(a0); submit(a1); submit(b0)
    order = [t.tx_hash for t in service.pool.candidate_order(10_000_000)]
    # 精确顺序：第一轮队首价格 a0=10 vs b0=40 -> b0；之后 alice 队首仍 a0。
    # 因此 b0(40), a0(10), a1(90)：a1 永远跟在 a0 之后，即使它价格更高。
    assert order == _hashes(service, [b0, a0, a1])


def test_candidate_tie_break_is_deterministic(service, fund, sign, submit):
    # 同价时按发送者地址升序，结果确定
    fund("alice", 10**20); fund("bob", 10**20)
    a0 = sign("alice", nonce=0, gas_price=10)
    b0 = sign("bob", nonce=0, gas_price=10)
    submit(a0); submit(b0)
    order1 = [t.tx_hash for t in service.pool.candidate_order(10**9)]
    order2 = [t.tx_hash for t in service.pool.candidate_order(10**9)]
    assert order1 == order2
    # 地址小的发送者排前
    senders_in_order = [t.sender for t in service.pool.candidate_order(10**9)]
    assert senders_in_order == sorted(senders_in_order)


def test_gas_limit_skips_full_sender_but_keeps_nonce_order(service, fund, sign, submit):
    fund("alice", 10**20); fund("bob", 10**20)
    # alice nonce0 附带 1000 字节零数据，固有 gas=21000+4000=25000
    a0 = sign("alice", nonce=0, gas_price=100, data=b"\x00" * 1000, gas_limit=25000)
    b0 = sign("bob", nonce=0, gas_price=1)                            # 21000 gas
    submit(a0); submit(b0)
    # gas_limit=21000：a0 放不下（队首更贵），跳过 alice，选 b0
    order = [t.tx_hash for t in service.pool.candidate_order(21000)]
    assert order == _hashes(service, [b0])


def test_propose_confirm_balances_and_nonces(service, fund, sign, submit):
    fund("alice", 10**18)
    fund("bob", 10**18)
    coinbase = "0x" + addr_for("miner").hex()
    # 先确保 miner 账户存在
    service.repo.ensure_account(coinbase, service.clock.now())
    tx = sign("alice", nonce=0, gas_price=10, value=1000, to=addr_for("bob"))
    submit(tx)
    block = service.chain.propose(coinbase=coinbase, request_id="t")
    assert block["number"] == 1
    # 提议后余额不动，交易 included
    assert service.repo.get_account("0x" + addr_for("alice").hex())["balance"] == str(10**18)
    service.chain.confirm(request_id="t")
    fee = 21000 * 10
    assert int(service.repo.get_account("0x" + addr_for("alice").hex())["balance"]) == 10**18 - 1000 - fee
    assert int(service.repo.get_account("0x" + addr_for("bob").hex())["balance"]) == 10**18 + 1000
    assert int(service.repo.get_account(coinbase)["balance"]) == fee
    assert service.repo.get_account("0x" + addr_for("alice").hex())["nonce"] == 1
    rec = service.repo.get_tx("0x" + tx.hash().hex())
    assert rec.status == "mined" and rec.block_number == 1 and rec.position == 0


def test_cannot_propose_two_open_blocks(service, fund, sign, submit):
    fund("alice", 10**20)
    submit(sign("alice", nonce=0, gas_price=10))
    submit(sign("alice", nonce=1, gas_price=10))
    service.chain.propose(request_id="t")
    with pytest.raises(BlockConflict):
        service.chain.propose(request_id="t")


def test_propose_empty_pool_raises(service):
    with pytest.raises(EmptyBlock):
        service.chain.propose(request_id="t")


def test_confirm_without_propose_raises(service):
    with pytest.raises(BlockNotProposed):
        service.chain.confirm(request_id="t")


def test_discard_returns_txs_to_pool(service, fund, sign, submit):
    fund("alice", 10**20)
    tx = sign("alice", nonce=0, gas_price=10)
    submit(tx)
    service.chain.propose(request_id="t")
    assert service.repo.get_tx("0x" + tx.hash().hex()).status == "included"
    service.chain.discard(request_id="t")
    rec = service.repo.get_tx("0x" + tx.hash().hex())
    assert rec.status == "pending"
    # 区块已删除，可重新提议
    block = service.chain.propose(request_id="t")
    assert block["number"] == 1


def test_rollback_restores_state_and_reclassifies(service, fund, sign, submit):
    fund("alice", 10**18); fund("bob", 10**18)
    tx = sign("alice", nonce=0, gas_price=10, value=1000, to=addr_for("bob"))
    submit(tx)
    service.chain.propose(request_id="t")
    service.chain.confirm(request_id="t")
    alice_before = 10**18 - 1000 - 21000 * 10
    assert int(service.repo.get_account("0x" + addr_for("alice").hex())["balance"]) == alice_before

    r = service.chain.rollback(1, request_id="t")
    assert r["reverted_blocks"] == [1]
    # 余额/nonce 完全恢复
    assert int(service.repo.get_account("0x" + addr_for("alice").hex())["balance"]) == 10**18
    assert service.repo.get_account("0x" + addr_for("alice").hex())["nonce"] == 0
    assert int(service.repo.get_account("0x" + addr_for("bob").hex())["balance"]) == 10**18
    rec = service.repo.get_tx("0x" + tx.hash().hex())
    assert rec.status == "pending" and rec.reason == "EXECUTABLE"
    assert rec.block_number is None and rec.position is None
    # 链高归零
    assert service.chain.head_number() == 0


def test_rollback_is_deterministic_redo(service, fund, sign, submit):
    fund("alice", 10**20); fund("bob", 10**20)
    submit(sign("alice", nonce=0, gas_price=20))
    submit(sign("bob", nonce=0, gas_price=10))
    b1 = service.chain.propose(request_id="t")
    order1 = b1["transactions"]
    service.chain.confirm(request_id="t")
    service.chain.rollback(1, request_id="t")
    b2 = service.chain.propose(request_id="t")
    assert b2["transactions"] == order1  # 回滚后重放顺序一致


def test_rollback_too_deep(service, fund, sign, submit):
    fund("alice", 10**20)
    submit(sign("alice", nonce=0, gas_price=10))
    service.chain.propose(request_id="t")
    service.chain.confirm(request_id="t")
    with pytest.raises(RollbackTooDeep):
        service.chain.rollback(2, request_id="t")


def test_rollback_refused_with_open_proposal(service, fund, sign, submit):
    fund("alice", 10**20)
    submit(sign("alice", nonce=0, gas_price=10))
    service.chain.propose(request_id="t")
    service.chain.confirm(request_id="t")
    submit(sign("alice", nonce=1, gas_price=10))
    service.chain.propose(request_id="t")
    with pytest.raises(BlockConflict):
        service.chain.rollback(1, request_id="t")


def test_two_blocks_numbering_and_parent_hash(service, fund, sign, submit):
    fund("alice", 10**20)
    submit(sign("alice", nonce=0, gas_price=10))
    b1 = service.chain.propose(request_id="t")
    service.chain.confirm(request_id="t")
    submit(sign("alice", nonce=1, gas_price=10))
    b2 = service.chain.propose(request_id="t")
    assert b2["number"] == 2
    assert b2["parent_hash"] == b1["hash"]
