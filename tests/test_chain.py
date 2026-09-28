"""链状态内核核心场景：连接/挂起、短分叉胜出、深分叉失败、权重平局、
跨分叉重复交易、确认深度/最终性查询、诊断事件。
"""

import pytest

from reorgindex import crypto
from reorgindex.errors import (
    ConsensusRuleError,
    DuplicateBlockError,
    FinalityReorgError,
    UnknownParentError,
)

from .conftest import make_tx


# --------------------------------------------------------------- 基本连接

def test_genesis_extends_and_tip_tracks(env, genesis):
    _, kernel, _ = env
    out = kernel.ingest(genesis, request_id="r1")
    assert out.status == "extended"
    assert kernel.tip_hash() == genesis["header"]["block_hash"]
    assert kernel.confirmation_depth(genesis["header"]["block_hash"]) == 1
    assert kernel.is_finalized(genesis["header"]["block_hash"]) is False


def test_blocks_join_by_parent_hash_in_order(env, make, genesis, keys):
    _, kernel, _ = env
    kernel.ingest(genesis)
    _, alice_pub, alice = keys["alice"]
    t1 = make_tx(keys["alice"][0], alice_pub, alice, 10, 0)
    b1 = make(genesis["header"]["block_hash"], 1, [t1], 1)
    out = kernel.ingest(b1)
    assert out.status == "extended"
    assert kernel.canonical_chain() == [
        genesis["header"]["block_hash"], b1["header"]["block_hash"]]
    assert kernel.get_balance(alice) == 10


def test_duplicate_block_is_idempotent_rejected(env, make, genesis):
    _, kernel, _ = env
    kernel.ingest(genesis)
    b1 = make(genesis["header"]["block_hash"], 1, [], 1)
    kernel.ingest(b1)
    with pytest.raises(DuplicateBlockError) as exc:
        kernel.ingest(b1)
    assert exc.value.category == "duplicate_block"


def test_height_gap_rejected(env, make, genesis):
    _, kernel, _ = env
    kernel.ingest(genesis)
    jump = make(genesis["header"]["block_hash"], 5, [], 1)  # 跳过高度 1..4
    with pytest.raises(ConsensusRuleError) as exc:
        kernel.ingest(jump)
    assert exc.value.category == "consensus_rule"


# --------------------------------------------------------------- 未知父挂起

def test_unknown_parent_pending_then_drains_in_order(env, make, keys):
    _, kernel, _ = env
    g = make(crypto.ZERO_HASH, 0, [], 1)
    b1 = make(g["header"]["block_hash"], 1, [], 1)
    b2 = make(b1["header"]["block_hash"], 2, [], 1)
    # 逆序到达
    out2 = kernel.ingest(b2)
    assert out2.status == "pending"
    assert out2.pending_count == 1
    out1 = kernel.ingest(b1)
    assert out1.status == "pending"  # b1 的父（创世）仍缺
    assert kernel.storage.pending_count() == 2
    outg = kernel.ingest(g)
    assert outg.status == "extended"
    assert kernel.storage.pending_count() == 0
    assert kernel.tip_hash() == b2["header"]["block_hash"]
    assert kernel.canonical_chain() == [g["header"]["block_hash"],
                                        b1["header"]["block_hash"],
                                        b2["header"]["block_hash"]]


def test_pending_block_not_visible_in_chain_until_connected(env, make):
    _, kernel, _ = env
    g = make(crypto.ZERO_HASH, 0, [], 1)
    orphan = make("ab" * 32, 1, [], 1)
    kernel.ingest(orphan)
    kernel.ingest(g)
    # 孤儿父永远缺失：链上看不到它
    assert kernel.tip_hash() == g["header"]["block_hash"]
    assert kernel.canonical_chain() == [g["header"]["block_hash"]]
    assert kernel.storage.pending_count() == 1


# --------------------------------------------------------------- 短分叉胜出

def test_short_heavier_fork_wins_with_rollback_range(env, make, genesis, keys):
    _, kernel, _ = env
    kernel.ingest(genesis)
    _, alice_pub, alice = keys["alice"]
    _, bob_pub, bob = keys["bob"]

    t1 = make_tx(keys["alice"][0], alice_pub, alice, 10, 0)
    a1 = make(genesis["header"]["block_hash"], 1, [t1], 1)
    t2 = make_tx(keys["alice"][0], alice_pub, alice, 20, 1)
    a2 = make(a1["header"]["block_hash"], 2, [t2], 1)
    kernel.ingest(a1)
    out_a2 = kernel.ingest(a2)
    assert out_a2.status == "extended"
    assert kernel.get_balance(alice) == 30

    # 分叉块：同高度权重 3 > 旧段（仅 a2）权重 1
    tb = make_tx(keys["alice"][0], alice_pub, bob, 5, 2)
    f2 = make(a1["header"]["block_hash"], 2, [tb], 3)
    out = kernel.ingest(f2)

    assert out.status == "switched"
    # 回滚区间精确：只撤 a2（高 2），不撤分叉点 a1，更不撤创世
    assert out.disconnected == [a2["header"]["block_hash"]]
    assert out.connected == [f2["header"]["block_hash"]]
    assert out.rollback_from_height == 2
    assert out.rollback_to_height == 2
    assert kernel.tip_hash() == f2["header"]["block_hash"]
    # 先撤旧后加新：a2 的 20 被撤回，保留 a1 的 10，新分支 bob +5
    assert kernel.get_balance(alice) == 10
    assert kernel.get_balance(bob) == 5
    assert not kernel.on_canonical(a2["header"]["block_hash"])
    assert kernel.on_canonical(f2["header"]["block_hash"])


def test_equal_weight_keeps_incumbent(env, make, genesis, keys):
    _, kernel, _ = env
    kernel.ingest(genesis)
    _, alice_pub, alice = keys["alice"]
    a1 = make(genesis["header"]["block_hash"], 1, [], 1)
    a2 = make(a1["header"]["block_hash"], 2,
              [make_tx(keys["alice"][0], alice_pub, alice, 1, 0)], 1)
    kernel.ingest(a1)
    kernel.ingest(a2)
    # 平局候选：同高度同权重，但携带不同交易故哈希不同
    f2 = make(a1["header"]["block_hash"], 2,
              [make_tx(keys["alice"][0], alice_pub, keys["bob"][2], 1, 1)], 1)
    out = kernel.ingest(f2)
    assert out.status == "fork_kept"
    assert kernel.tip_hash() == a2["header"]["block_hash"]


def test_weaker_fork_does_not_switch(env, make, genesis, keys):
    _, kernel, _ = env
    kernel.ingest(genesis)
    _, alice_pub, alice = keys["alice"]
    a1 = make(genesis["header"]["block_hash"], 1, [], 2)
    a2 = make(a1["header"]["block_hash"], 2,
              [make_tx(keys["alice"][0], alice_pub, alice, 1, 0)], 2)
    kernel.ingest(a1)
    kernel.ingest(a2)
    f2 = make(a1["header"]["block_hash"], 2,
              [make_tx(keys["alice"][0], alice_pub, keys["bob"][2], 1, 1)], 1)  # 1 < 旧段 2
    out = kernel.ingest(f2)
    assert out.status == "fork_kept"
    assert kernel.tip_hash() == a2["header"]["block_hash"]


# --------------------------------------------------------------- 最终性深度

def test_finality_boundary_and_confirmation_depth(env, make, genesis):
    _, kernel, _ = env
    D = kernel.settings.finality_depth
    kernel.ingest(genesis)
    blocks = [genesis]
    for h in range(1, D + 3):
        nxt = make(blocks[-1]["header"]["block_hash"], h, [], 1)
        kernel.ingest(nxt)
        blocks.append(nxt)
    tip_h = D + 2
    assert kernel._require_block(kernel.tip_hash())["height"] == tip_h
    # 确认数：链尖 1，越深越大
    assert kernel.confirmation_depth(blocks[tip_h]["header"]["block_hash"]) == 1
    assert kernel.confirmation_depth(blocks[0]["header"]["block_hash"]) == tip_h + 1
    # 高 h 最终确定 <=> tip-h >= D
    assert kernel.is_finalized(blocks[tip_h - D]["header"]["block_hash"]) is True
    assert kernel.is_finalized(blocks[tip_h - D + 1]["header"]["block_hash"]) is False
    assert kernel.finalized_height() == tip_h - D


def test_deep_fork_rejected_and_state_untouched(env, make, genesis, keys):
    storage, kernel, _ = env
    D = kernel.settings.finality_depth
    kernel.ingest(genesis)
    _, alice_pub, alice = keys["alice"]
    a1 = make(genesis["header"]["block_hash"], 1,
              [make_tx(keys["alice"][0], alice_pub, alice, 10, 0)], 1)
    kernel.ingest(a1)
    chain = [a1]
    for h in range(2, D + 3):  # 到高度 D+2
        nxt = make(chain[-1]["header"]["block_hash"], h,
                   [make_tx(keys["alice"][0], alice_pub, alice, 1, h)], 1)
        kernel.ingest(nxt)
        chain.append(nxt)
    tip_before = kernel.tip_hash()
    balance_before = kernel.get_balance(alice)

    # 从高度 1 分叉：回滚高度 2..D+2 = D+1 块，越界
    deep = make(a1["header"]["block_hash"], 2,
                [make_tx(keys["bob"][0], keys["bob"][1], keys["bob"][2], 9999, 0)], 10_000)
    with pytest.raises(FinalityReorgError) as exc:
        kernel.ingest(deep)
    assert exc.value.category == "finality_reorg"
    assert exc.value.context["rollback_count"] == D + 1
    # 链尖、余额、权威链完全不变；被拒块不入 blocks 表
    assert kernel.tip_hash() == tip_before
    assert kernel.get_balance(alice) == balance_before
    assert storage.get_block(deep["header"]["block_hash"]) is None


def test_reorg_at_exactly_depth_is_allowed(env, make, genesis):
    """回滚数恰好等于最终性深度 D：允许；D+1：拒绝（见深分叉测试）。"""

    _, kernel, _ = env
    D = kernel.settings.finality_depth
    kernel.ingest(genesis)
    a1 = make(genesis["header"]["block_hash"], 1, [], 1)
    kernel.ingest(a1)
    # 旧链：高度 2..D+1（D 块，权重 1）
    cur = a1
    for h in range(2, D + 2):
        cur = make(cur["header"]["block_hash"], h, [], 1)
        kernel.ingest(cur)

    # 竞争分叉先以"父未知"形式挂起；旧链长好后再逐块补齐，使整条分叉在
    # 最后一块排空时一次性触发回滚恰好 D 个区块的重组。
    new_chain = []
    parent = a1["header"]["block_hash"]
    for h in range(2, D + 2):
        nxt = make(parent, h, [], 2)
        new_chain.append(nxt)
        parent = nxt["header"]["block_hash"]
    # 把分叉高 3..D+1 挂起（其父指向尚不存在的新分支块）
    for nxt in new_chain[1:]:
        assert kernel.ingest(nxt).status == "pending"
    # 提交分叉高 2 -> 排空，链尖最终落到新分支
    out = kernel.ingest(new_chain[0])
    assert out.status == "switched"
    # 排空可能分多次切换；汇总总回滚高度区间应覆盖旧链高度 2..D+1
    all_disc = list(out.disconnected)
    assert 2 in [kernel._require_block(h)["height"] for h in all_disc]
    assert D + 1 in [kernel._require_block(h)["height"] for h in all_disc]
    assert kernel.tip_hash() == new_chain[-1]["header"]["block_hash"]
    assert kernel._require_block(kernel.tip_hash())["height"] == D + 1


# --------------------------------------------------------------- 跨分叉重复交易

def test_same_tx_on_both_branches_contributes_once(env, make, genesis, keys):
    storage, kernel, _ = env
    kernel.ingest(genesis)
    carol = keys["carol"]
    shared = make_tx(keys["alice"][0], keys["alice"][1], carol, 100, 0, "shared")

    a1 = make(genesis["header"]["block_hash"], 1, [shared], 1)
    a2 = make(a1["header"]["block_hash"], 2,
              [make_tx(keys["alice"][0], keys["alice"][1], keys["alice"][2], 4, 1)], 1)
    kernel.ingest(a1)
    kernel.ingest(a2)
    assert kernel.get_balance(carol) == 100

    # 新分叉重复携带同一笔 shared（同 tx_id 同签名体）
    f2 = make(a1["header"]["block_hash"], 2,
              [shared, make_tx(keys["alice"][0], keys["alice"][1], keys["bob"][2], 8, 2)], 3)
    out = kernel.ingest(f2)
    assert out.status == "switched"
    assert kernel.get_balance(carol) == 100          # 不是 200
    assert kernel.get_balance(keys["bob"][2]) == 8
    # 唯一约束层面只有一条 shared 贡献
    rows = storage._conn.execute(
        "SELECT COUNT(*) FROM derived_contributions WHERE tx_id=?", (shared["tx_id"],)
    ).fetchone()
    assert rows[0] == 1
    assert storage.contribution_count() == 2  # shared + bob 的 8


def test_duplicate_tx_within_same_chain_contributes_once(env, make, genesis, keys):
    storage, kernel, _ = env
    kernel.ingest(genesis)
    carol = keys["carol"]
    shared = make_tx(keys["alice"][0], keys["alice"][1], carol, 50, 0)
    b1 = make(genesis["header"]["block_hash"], 1, [shared], 1)
    b2 = make(b1["header"]["block_hash"], 2, [shared], 1)  # 同链再次包含
    kernel.ingest(b1)
    kernel.ingest(b2)
    assert kernel.get_balance(carol) == 50
    assert storage.contribution_count() == 1


# --------------------------------------------------------------- 诊断

def test_diagnostics_carry_request_id_and_state(env, make, genesis):
    _, kernel, diagnostics = env
    kernel.ingest(genesis, request_id="req-xyz")
    events = kernel.storage.recent_diag(50)
    accepted = [e for e in events if e["event"] == "block_accepted"]
    assert accepted, "应记录 block_accepted"
    e = accepted[0]
    assert e["request_id"] == "req-xyz"
    assert e["event_id"].startswith("evt-")
    assert e["payload"]["block_hash"] == genesis["header"]["block_hash"]
    assert e["payload"]["height"] == 0


def test_finality_rejection_has_diagnostic_reason(env, make, genesis, keys):
    storage, kernel, _ = env
    D = kernel.settings.finality_depth
    kernel.ingest(genesis)
    cur = genesis
    for h in range(1, D + 2):
        cur = make(cur["header"]["block_hash"], h, [], 1)
        kernel.ingest(cur)
    deep = make(genesis["header"]["block_hash"], 1, [], 10_000)
    with pytest.raises(FinalityReorgError):
        kernel.ingest(deep, request_id="req-deep")
    rejected = [e for e in storage.recent_diag(100)
                if e["event"] == "block_rejected" and e["payload"].get("reason") == "finality_reorg"]
    assert rejected
    assert rejected[0]["request_id"] == "req-deep"
    assert rejected[0]["payload"]["rollback_count"] == D + 1
