"""区块确认深度与回滚重分类的内核测试。"""

from __future__ import annotations

import pytest

from local_txpool.core.models import ErrorCode, TxStatus
from tests.conftest import make_tx

GWEI = 1_000_000_000


def _fund(kernel, addresses, *names):
    for n in names:
        kernel.create_or_fund_account(addresses[n], 10**18)


def test_rollback_refunds_and_reenters(kernel, keys, addresses, config):
    config.finality.confirmation_depth = 5
    _fund(kernel, addresses, "alice", "bob")
    a0 = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    b0 = make_tx(keys["bob"], nonce=0, gas_price=9 * GWEI)
    kernel.submit_transaction(a0)
    kernel.submit_transaction(b0)

    alice_before = kernel.get_account(addresses["alice"])
    block, plan, _ = kernel.propose_block()
    assert plan.tx_hashes() == [b0.tx_hash, a0.tx_hash]

    alice_after = kernel.get_account(addresses["alice"])
    assert alice_after.balance < alice_before.balance

    result = kernel.rollback_to(0)
    assert set(result["reentered"]) == {a0.tx_hash, b0.tx_hash}

    # 退款、nonce 回退
    alice_rb = kernel.get_account(addresses["alice"])
    assert alice_rb.balance == alice_before.balance
    assert alice_rb.nonce == 0

    # 交易重新成为 pending，候选顺序可复现
    pool = kernel.list_pool()
    assert {p["tx_hash"] for p in pool["pending"]} == {
        a0.tx_hash, b0.tx_hash
    }
    kernel.assert_integrity()


def test_finalized_blocks_cannot_roll_back(kernel, keys, addresses, config):
    config.finality.confirmation_depth = 1
    _fund(kernel, addresses, "alice", "bob")
    # 两块，每块一笔：块 1 (alice)、块 2 (bob)。
    kernel.submit_transaction(make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI))
    kernel.propose_block()
    kernel.submit_transaction(make_tx(keys["bob"], nonce=0, gas_price=3 * GWEI))
    kernel.propose_block()  # 高度 2：块 1 在链头下方 1 -> 最终化
    with pytest.raises(Exception) as exc:  # noqa: PT011
        kernel.rollback_to(0)
    assert getattr(exc.value, "code", None) is (
        ErrorCode.BLOCK_ROLLBACK_FINALIZED
    )
    kernel.assert_integrity()


def test_confirm_marks_depth(kernel, keys, addresses, config):
    config.finality.confirmation_depth = 2
    _fund(kernel, addresses, "alice")
    txs = [make_tx(keys["alice"], nonce=i, gas_price=2 * GWEI) for i in range(4)]
    # 每笔单独提交+成块，形成高度 1..4
    for tx in txs:
        kernel.submit_transaction(tx)
        kernel.propose_block()

    # 高度 4：区块 1、2（<=4-2）最终确认
    assert kernel.get_tx(txs[0].tx_hash).status is TxStatus.CONFIRMED
    assert kernel.get_tx(txs[1].tx_hash).status is TxStatus.CONFIRMED
    assert kernel.get_tx(txs[3].tx_hash).status is TxStatus.PROPOSED
    kernel.assert_integrity()


def test_rollback_reclassifies_gap(kernel, keys, addresses, config):
    """回滚后，被重入的交易仍受 nonce 缺口规则约束。"""
    config.finality.confirmation_depth = 5
    _fund(kernel, addresses, "alice")
    t0 = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    t1 = make_tx(keys["alice"], nonce=1, gas_price=2 * GWEI)
    kernel.submit_transaction(t0)
    kernel.submit_transaction(t1)
    kernel.propose_block()
    kernel.rollback_to(0)
    # 两笔都应重新 pending（连续前缀恢复）
    statuses = {
        kernel.get_tx(t.tx_hash).status for t in (t0, t1)
    }
    assert statuses == {TxStatus.PENDING}
    kernel.assert_integrity()
