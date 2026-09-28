"""交易池核心机制测试：断言具体分类结果与具体失败类别。"""

from __future__ import annotations

import pytest

from localtxpool.errors import (
    AccountQueueFull,
    AlreadyKnown,
    NonceTooFar,
    NonceTooLow,
    PoolFull,
    ReplacementUnderpriced,
    Underpriced,
)
from localtxpool.core import (
    PENDING, QUEUED,
    REASON_NONCE_GAP, REASON_GAP_AFFORDABILITY,
    REASON_INSUFFICIENT_FUNDS, REASON_EXECUTABLE,
)
from tests.conftest import addr_for


def _status(service, tx):
    rec = service.repo.get_tx("0x" + tx.hash().hex())
    return rec.status, rec.reason


# --------------------------------------------------------------------------- #
# nonce 前缀：费用再高不能跳过缺口
# --------------------------------------------------------------------------- #
def test_high_fee_cannot_skip_nonce_gap(service, fund, sign, submit):
    fund("alice", 10**20)
    gap = sign("alice", nonce=5, gas_price=1_000_000, to=addr_for("bob"))
    r = submit(gap)
    assert r["status"] == QUEUED
    assert r["reason"] == REASON_NONCE_GAP

    # 候选区块为空（只有带缺口的交易）
    preview = service.chain.candidate_preview()
    assert preview["order"] == []

    # 即使候选 gas 再大也没用
    assert service.chain.candidate_preview(gas_limit=10**9)["order"] == []


def test_prefix_walks_consecutive_nonces(service, fund, sign, submit):
    fund("alice", 10**20)
    t0 = submit(sign("alice", nonce=0, gas_price=5))
    t2 = submit(sign("alice", nonce=2, gas_price=100))  # 缺口
    t1 = submit(sign("alice", nonce=1, gas_price=5))
    assert t0["status"] == PENDING
    assert t1["status"] == PENDING
    # nonce2 在 0、1 都到位后仍 pending（前缀连续）
    rec2 = service.repo.get_tx(t2["tx_hash"])
    assert (rec2.status, rec2.reason) == (PENDING, REASON_EXECUTABLE)


def test_gap_reason_changes_when_prefix_becomes_affordable(service, fund, sign, submit):
    # 初始：只有 nonce1，缺口
    fund("alice", 10**20)
    t1 = submit(sign("alice", nonce=1, gas_price=100))
    assert t1["reason"] == REASON_NONCE_GAP
    # 补 nonce0 且负担得起 -> nonce1 变 pending
    submit(sign("alice", nonce=0, gas_price=1))
    rec = service.repo.get_tx(t1["tx_hash"])
    assert (rec.status, rec.reason) == (PENDING, REASON_EXECUTABLE)


# --------------------------------------------------------------------------- #
# 余额断裂
# --------------------------------------------------------------------------- #
def test_unaffordable_head_breaks_prefix(service, fund, sign, submit):
    fund("alice", 100_000)  # 不够 nonce0 的巨额 value
    # nonce1 先到（缺口）
    t1 = submit(sign("alice", nonce=1, gas_price=100, value=0))
    assert t1["reason"] == REASON_NONCE_GAP
    # nonce0 买不起：前缀在 0 断裂，nonce1 的理由变为 GAP_AFFORDABILITY
    t0 = submit(sign("alice", nonce=0, gas_price=10, value=10**18))
    assert t0["status"] == QUEUED and t0["reason"] == REASON_INSUFFICIENT_FUNDS
    rec1 = service.repo.get_tx(t1["tx_hash"])
    assert (rec1.status, rec1.reason) == (QUEUED, REASON_GAP_AFFORDABILITY)
    # 候选为空
    assert service.chain.candidate_preview()["order"] == []


def test_funding_repairs_prefix(service, fund, sign, submit):
    fund("alice", 0)
    t0 = submit(sign("alice", nonce=0, gas_price=10, value=5))
    assert t0["reason"] == REASON_INSUFFICIENT_FUNDS
    # 充值（/admin/fund 同样会触发重分类）
    addr = "0x" + addr_for("alice").hex()
    service.repo.adjust_balance(addr, 10**18, service.clock.now())
    res = service.pool.classify_sender(addr, request_id="fund-test")
    assert res["pending"] == [t0["tx_hash"]]


def test_prefix_projects_costs_across_nonces(service, fund, sign, submit):
    # 每笔最多消耗 21000*10=210000；余额只够前两笔 => 第三笔必须 queued，
    # 即使它本身金额为 0、单笔看“余额足够”，前缀也不能越过累计花费。
    fund("alice", 210000 * 2)
    t0 = submit(sign("alice", nonce=0, gas_price=10, value=0))
    t1 = submit(sign("alice", nonce=1, gas_price=10, value=0))
    t2 = submit(sign("alice", nonce=2, gas_price=10, value=0))
    t3 = submit(sign("alice", nonce=3, gas_price=10, value=0))
    assert t0["status"] == PENDING
    assert t1["status"] == PENDING
    rec2 = service.repo.get_tx(t2["tx_hash"])
    # 第三笔是前缀中第一个累计负担不起的交易本身 -> INSUFFICIENT_FUNDS
    assert (rec2.status, rec2.reason) == (QUEUED, REASON_INSUFFICIENT_FUNDS)
    # 它之后的 nonce3 -> GAP_AFFORDABILITY
    rec3 = service.repo.get_tx(t3["tx_hash"])
    assert (rec3.status, rec3.reason) == (QUEUED, REASON_GAP_AFFORDABILITY)
    # 候选区块只能包含前两笔（第三笔不得被选入而跳过累计余额断裂）
    order = [o["tx_hash"] for o in service.chain.candidate_preview()["order"]]
    assert order == [t0["tx_hash"], t1["tx_hash"]]


# --------------------------------------------------------------------------- #
# 替换
# --------------------------------------------------------------------------- #
def test_replacement_requires_bump(service_factory, sign):
    service, fund, submit = service_factory(price_bump_pct=10)
    fund("alice", 10**20)
    submit(sign("alice", nonce=0, gas_price=100, value=1))
    # 100 -> 109 不满足（需要 >=110）
    with pytest.raises(ReplacementUnderpriced) as ei:
        submit(sign("alice", nonce=0, gas_price=109, value=2))
    assert ei.value.details["required_gas_price"] == 110
    # 110 恰好满足
    r = submit(sign("alice", nonce=0, gas_price=110, value=3))
    assert r["status"] == PENDING
    # 旧交易已归档为 replaced，且指向新哈希
    old = service.repo.list_sender("0x" + addr_for("alice").hex(), ("replaced",))
    assert len(old) == 1
    assert old[0].reason == "REPLACED_PRICE_BUMP"
    assert old[0].replaced_by == r["tx_hash"]
    # 同 nonce 只有一条有效交易
    active = service.repo.list_sender("0x" + addr_for("alice").hex(), (PENDING, QUEUED))
    assert len(active) == 1 and active[0].gas_price == 110


def test_replacement_bump_rounds_up(service_factory, sign):
    service, fund, submit = service_factory(price_bump_pct=10)
    fund("alice", 10**20)
    submit(sign("alice", nonce=0, gas_price=1, value=1))
    # ceil(1*1.1)=2
    r = submit(sign("alice", nonce=0, gas_price=2, value=2))
    assert r["status"] == PENDING


def test_same_hash_is_already_known(service, fund, sign, submit):
    fund("alice", 10**20)
    tx = sign("alice", nonce=0, gas_price=10)
    submit(tx)
    with pytest.raises(AlreadyKnown):
        submit(tx)


def test_replacing_pending_with_unaffordable_rejected(service, fund, sign, submit):
    # 账户只够便宜的 nonce0；高价替换者自己买不起 -> 拒绝替换
    fund("alice", 21000 * 10 + 100)
    submit(sign("alice", nonce=0, gas_price=10, value=0))
    with pytest.raises(Exception) as ei:
        # 高价 + 高 value 超出余额
        submit(sign("alice", nonce=0, gas_price=100, value=10**18))
    assert ei.value.code == "INSUFFICIENT_FUNDS"


# --------------------------------------------------------------------------- #
# nonce 范围 / gas 地板
# --------------------------------------------------------------------------- #
def test_nonce_too_low_after_confirm(service, fund, sign, submit):
    fund("alice", 10**20)
    submit(sign("alice", nonce=0, gas_price=10, value=1))
    service.chain.propose(request_id="t")
    service.chain.confirm(request_id="t")
    # 另一笔不同的 nonce0 交易（value 不同 => 哈希不同）必须按 nonce 太低拒绝
    with pytest.raises(Exception) as ei:
        submit(sign("alice", nonce=0, gas_price=10, value=2))
    assert ei.value.code == "NONCE_TOO_LOW"


def test_nonce_too_far(service_factory, sign):
    service, fund, submit = service_factory(max_future_nonce=4)
    fund("alice", 10**20)
    with pytest.raises(NonceTooFar):
        submit(sign("alice", nonce=5, gas_price=10))


def test_zero_gas_price_rejected(service, fund, sign, submit):
    fund("alice", 10**20)
    with pytest.raises(Underpriced):
        submit(sign("alice", nonce=0, gas_price=0))


# --------------------------------------------------------------------------- #
# 容量淘汰
# --------------------------------------------------------------------------- #
def test_global_queue_eviction(service_factory, sign):
    service, fund, submit = service_factory(max_global_queued=2, max_account_queued=100)
    # 三个无余额账户，交易全部 queued
    fund("alice", 0)
    fund("bob", 0)
    fund("carol", 0)
    submit(sign("alice", nonce=0, gas_price=100))
    submit(sign("bob", nonce=0, gas_price=50))
    # Carol 更贵：插入时应淘汰最便宜的 queued（bob=50）
    submit(sign("carol", nonce=0, gas_price=200))
    evicted = service.repo.list_all(("evicted",))
    assert len(evicted) == 1
    assert evicted[0].gas_price == 50  # bob 被淘汰
    assert evicted[0].reason == "EVICTED_QUEUE_GLOBAL"
    # 索引仍自洽：各账户活跃 queued 数量
    assert service.repo.count_status((QUEUED,)) == 2


def test_incoming_cheap_does_not_evict(service_factory, sign):
    service, fund, submit = service_factory(max_global_queued=2, max_account_queued=100)
    fund("alice", 0); fund("bob", 0); fund("carol", 0)
    submit(sign("alice", nonce=0, gas_price=100))
    submit(sign("bob", nonce=0, gas_price=200))
    # 更便宜的新交易无法挤掉任何 queued -> POOL_FULL
    with pytest.raises(PoolFull):
        submit(sign("carol", nonce=0, gas_price=50))


def test_account_queue_cap(service_factory, sign):
    service, fund, submit = service_factory(max_account_queued=2, max_global_queued=1000)
    fund("alice", 0)
    submit(sign("alice", nonce=1, gas_price=100))
    submit(sign("alice", nonce=2, gas_price=200))
    # 第三笔更贵：淘汰本账户最便宜（nonce1,gp100）
    submit(sign("alice", nonce=3, gas_price=300))
    evicted = service.repo.list_all(("evicted",))
    assert [e.nonce for e in evicted] == [1]
    assert evicted[0].reason == "EVICTED_QUEUE_ACCOUNT"


# --------------------------------------------------------------------------- #
# 过期
# --------------------------------------------------------------------------- #
def test_expiry_frees_slot_and_keeps_index_consistent(service, fund, sign, submit):
    fund("alice", 10**20)
    r = submit(sign("alice", nonce=0, gas_price=10))
    service.clock.advance(4000)
    expired = service.pool.reap_expired(request_id="ttl")
    assert r["tx_hash"] in expired
    rec = service.repo.get_tx(r["tx_hash"])
    assert rec.status == "expired" and rec.reason == "EXPIRED_TTL"
    # 过期释放活跃槽位：同 nonce 新交易可进入（即使价格更低，因为不是替换）
    r2 = submit(sign("alice", nonce=0, gas_price=5, value=1))
    assert r2["status"] in (PENDING, QUEUED)
    # 旧交易仍是 expired，没有被覆盖；唯一索引不冲突
    assert service.repo.get_tx(r["tx_hash"]).status == "expired"


def test_included_tx_can_expire_then_propose_skips_it(service, fund, sign, submit):
    fund("alice", 10**20)
    submit(sign("alice", nonce=0, gas_price=10))
    service.chain.propose(request_id="t")
    service.clock.advance(4000)
    # 新提议前先收割：上一个 proposed 仍打开 -> 直接 reap 标记
    service.pool.reap_expired(request_id="t")
    # 丢弃旧提议，过期交易不再回到 pending
    service.chain.discard(request_id="t")
    rec = service.repo.list_all(("expired",))
    assert len(rec) == 1
    assert service.chain.candidate_preview()["order"] == []
