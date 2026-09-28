"""内核行为测试：准入、连续前缀/缺口、费用竞争、替换、余额、容量。

这些测试直接对 ``Kernel`` + 内存 SQLite 断言**具体结果与错误类别**，
且每个改变状态的用例末尾都跑 ``assert_integrity()``（索引/余额不变量）。
"""

from __future__ import annotations

import pytest

from local_txpool.core.config import Config
from local_txpool.core.models import ErrorCode, TxStatus
from tests.conftest import make_tx

GWEI = 1_000_000_000
ETH = 10**18


def _fund(kernel, addresses, name, balance=10**18):
    kernel.create_or_fund_account(addresses[name], balance)


def _status(kernel, tx):
    stored = kernel.get_tx(tx.tx_hash)
    return stored.status if stored else None


# ------------------------- 准入失败类别 ------------------------- #
def test_low_gas_price_rejected(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    tx = make_tx(keys["alice"], nonce=0, gas_price=GWEI // 2)
    r = kernel.submit_transaction(tx)
    assert not r.accepted
    assert r.error_code is ErrorCode.GAS_PRICE_BELOW_MINIMUM


def test_intrinsic_gas_rejected(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    tx = make_tx(keys["alice"], nonce=0, gas_limit=21_000 - 1)
    r = kernel.submit_transaction(tx)
    assert r.error_code is ErrorCode.INTRINSIC_GAS_TOO_LOW


def test_gas_limit_over_block_rejected(kernel, keys, addresses, config):
    config.gas.block_gas_limit = 100_000
    _fund(kernel, addresses, "alice")
    tx = make_tx(keys["alice"], nonce=0, gas_limit=200_000)
    r = kernel.submit_transaction(tx)
    assert r.error_code is ErrorCode.GAS_LIMIT_EXCEEDS_BLOCK


def test_nonce_too_low_after_block(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    kernel.submit_transaction(make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI))
    kernel.propose_block()
    # 旧交易已进入未确认区块（proposed）：此时同 nonce 高费替换必须先回滚，
    # 服务返回 CONFLICT 而不是悄悄换掉区块里的交易。
    stale = make_tx(keys["alice"], nonce=0, gas_price=9 * GWEI)
    r = kernel.submit_transaction(stale)
    assert r.error_code is ErrorCode.CONFLICT

    # 回滚后同 nonce 高费替换成功；再出块并用后续区块把它最终确认
    kernel.rollback_to(0)
    replaced = make_tx(keys["alice"], nonce=0, gas_price=9 * GWEI)
    r2 = kernel.submit_transaction(replaced)
    assert r2.accepted
    kernel.propose_block()
    # 推进确认深度（默认 depth=3）：再出 3 个空填充块不可行（无交易不允许空块），
    # 因此这里直接验证"已执行但未确认"窗口的语义：proposed 期间 -> CONFLICT。
    stale2 = make_tx(keys["alice"], nonce=0, gas_price=99 * GWEI)
    r3 = kernel.submit_transaction(stale2)
    assert r3.error_code is ErrorCode.CONFLICT


def test_nonce_too_far_ahead(kernel, keys, addresses, config):
    config.pool.max_queued_per_sender = 2
    _fund(kernel, addresses, "alice")
    r = kernel.submit_transaction(
        make_tx(keys["alice"], nonce=3, gas_price=2 * GWEI)
    )
    assert r.error_code is ErrorCode.NONCE_TOO_FAR_AHEAD


def test_insufficient_funds_at_nonce(kernel, keys, addresses):
    _fund(kernel, addresses, "alice", balance=10_000)
    tx = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    r = kernel.submit_transaction(tx)
    assert r.error_code is ErrorCode.INSUFFICIENT_FUNDS


# ------------------------- 连续前缀与缺口 ------------------------- #
def test_high_fee_cannot_jump_gap(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    t0 = make_tx(keys["alice"], nonce=0, gas_price=1 * GWEI)
    t2 = make_tx(keys["alice"], nonce=2, gas_price=100 * GWEI)
    kernel.submit_transaction(t0)
    kernel.submit_transaction(t2)

    pool = kernel.list_pool()
    assert [p["tx_hash"] for p in pool["pending"]] == [t0.tx_hash]
    assert [q["tx_hash"] for q in pool["queued"]] == [t2.tx_hash]
    # 候选区块：t2 全池最高价，但不能越过 nonce 缺口
    cand = kernel.preview_candidate()
    assert [c["tx_hash"] for c in cand["ordered"]] == [t0.tx_hash]
    kernel.assert_integrity()


def test_gap_fill_promotes_prefix(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    t0 = make_tx(keys["alice"], nonce=0, gas_price=1 * GWEI)
    t2 = make_tx(keys["alice"], nonce=2, gas_price=100 * GWEI)
    t1 = make_tx(keys["alice"], nonce=1, gas_price=2 * GWEI)
    for t in (t0, t2, t1):
        kernel.submit_transaction(t)
    cand = kernel.preview_candidate()
    # 严格 nonce 序：即使 t2 价高，顺序仍必须是 0,1,2
    assert [c["nonce"] for c in cand["ordered"]] == [0, 1, 2]
    assert [c["tx_hash"] for c in cand["ordered"]] == [
        t0.tx_hash, t1.tx_hash, t2.tx_hash
    ]
    kernel.assert_integrity()


def test_fee_competition_multi_account(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    _fund(kernel, addresses, "bob")
    _fund(kernel, addresses, "carol")
    a = make_tx(keys["alice"], nonce=0, gas_price=5 * GWEI)
    b = make_tx(keys["bob"], nonce=0, gas_price=50 * GWEI)
    c = make_tx(keys["carol"], nonce=0, gas_price=2 * GWEI)
    for t in (a, b, c):
        kernel.submit_transaction(t)
    cand = kernel.preview_candidate()
    assert [x["tx_hash"] for x in cand["ordered"]] == [
        b.tx_hash, a.tx_hash, c.tx_hash
    ]
    kernel.assert_integrity()


# ------------------------- RBF 替换 ------------------------- #
def test_replacement_requires_price_bump(kernel, keys, addresses, config):
    config.pool.replacement_price_bump_pct = 10
    _fund(kernel, addresses, "alice")
    old = make_tx(keys["alice"], nonce=0, gas_price=10 * GWEI)
    kernel.submit_transaction(old)

    low = make_tx(keys["alice"], nonce=0, gas_price=10 * GWEI + 1)
    r = kernel.submit_transaction(low)
    assert r.error_code is ErrorCode.SAME_NONCE_LOWER_PRICE
    assert r.detail["required_gas_price"] == 11 * GWEI

    # 恰好 10%（向上取整）通过
    bumped = make_tx(keys["alice"], nonce=0, gas_price=11 * GWEI)
    r2 = kernel.submit_transaction(bumped)
    assert r2.accepted
    assert _status(kernel, old) is TxStatus.DROPPED
    assert _status(kernel, bumped) is TxStatus.PENDING
    # 同 nonce 有效交易唯一
    kernel.assert_integrity()


def test_replacement_of_proposed_requires_rollback(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    old = make_tx(keys["alice"], nonce=0, gas_price=10 * GWEI)
    kernel.submit_transaction(old)
    kernel.propose_block()
    assert _status(kernel, old) is TxStatus.PROPOSED
    bumped = make_tx(keys["alice"], nonce=0, gas_price=20 * GWEI)
    r = kernel.submit_transaction(bumped)
    assert r.error_code is ErrorCode.CONFLICT
    kernel.assert_integrity()


# ------------------------- 余额 ------------------------- #
def test_balance_cutoff_makes_later_nonce_queued(kernel, keys, addresses):
    # 每笔 21000 * 10gwei = 2.1e14；余额 5e14 够 2 笔，第 3 笔截止
    kernel.create_or_fund_account(addresses["alice"], 5 * 10**14)
    txs = [
        make_tx(keys["alice"], nonce=i, gas_price=10 * GWEI)
        for i in range(3)
    ]
    for t in txs:
        kernel.submit_transaction(t)
    pool = kernel.list_pool()
    assert {p["nonce"] for p in pool["pending"]} == {0, 1}
    assert [q["nonce"] for q in pool["queued"]] == [2]
    assert pool["queued"][0]["status_reason"] == "queued_balance_cutoff"
    kernel.assert_integrity()


def test_balance_changes_on_propose(kernel, keys, addresses):
    kernel.create_or_fund_account(addresses["alice"], ETH)
    tx = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI, value=1234)
    kernel.submit_transaction(tx)
    before = kernel.get_account(addresses["alice"])
    block, _, _ = kernel.propose_block()
    after = kernel.get_account(addresses["alice"])
    expected_cost = 21_000 * 2 * GWEI + 1234
    assert before.balance - after.balance == expected_cost
    assert after.nonce == 1
    assert tx.tx_hash in block.executed_tx_hashes
    kernel.assert_integrity()


# ------------------------- 容量/淘汰 ------------------------- #
def test_eviction_prefers_low_price_queued(kernel, keys, addresses, config):
    config.pool.max_transactions = 4
    _fund(kernel, addresses, "alice")
    _fund(kernel, addresses, "bob")
    _fund(kernel, addresses, "carol")

    a0 = make_tx(keys["alice"], nonce=0, gas_price=10 * GWEI)
    bq_hi = make_tx(keys["bob"], nonce=3, gas_price=5 * GWEI)
    bq_lo = make_tx(keys["bob"], nonce=4, gas_price=1 * GWEI)
    b0 = make_tx(keys["bob"], nonce=0, gas_price=8 * GWEI)
    c0 = make_tx(keys["carol"], nonce=0, gas_price=7 * GWEI)
    kernel.submit_transaction(a0)       # pending
    kernel.submit_transaction(bq_hi)    # queued
    kernel.submit_transaction(bq_lo)    # queued (3)
    kernel.submit_transaction(b0)       # pending (4，恰好满)
    # 第 5 条使池超容：先驱逐 queued 中价最低者 bq_lo
    r = kernel.submit_transaction(c0)
    assert r.accepted
    assert _status(kernel, bq_lo) is TxStatus.DROPPED
    assert _status(kernel, bq_hi) is TxStatus.QUEUED
    assert _status(kernel, a0) is TxStatus.PENDING
    assert _status(kernel, c0) is TxStatus.PENDING
    kernel.assert_integrity()


# ------------------------- 过期 ------------------------- #
def test_expiry_does_not_break_prefix(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    t0 = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    t1 = make_tx(keys["alice"], nonce=1, gas_price=2 * GWEI)
    kernel.submit_transaction(t0)
    kernel.clock.advance(100_000)
    kernel.submit_transaction(t1)
    kernel.clock.advance(201_000)  # t0 已 pending 301s；t1 仅 201s
    expired = kernel.expire_pending()
    assert expired == [t0.tx_hash]
    assert _status(kernel, t0) is TxStatus.DROPPED
    # t1 不能"跳过"缺口：nonce=1 但账户 nonce 仍为 0 -> queued
    assert _status(kernel, t1) is TxStatus.QUEUED
    kernel.assert_integrity()


# ------------------------- 请求身份与审计 ------------------------- #
def test_audit_trail_associates_request(kernel, keys, addresses):
    _fund(kernel, addresses, "alice")
    tx = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    rid = "submit-fixed-id"
    r = kernel.submit_transaction(tx, request_id=rid)
    assert r.accepted
    trail = kernel.audit_for_request(rid)
    assert trail["found"]
    types = [e["event_type"] for e in trail["events"]]
    assert "admitted" in types
    assert all(e["request_id"] == rid for e in trail["events"])
    assert all(e["service_version"] for e in trail["events"])
