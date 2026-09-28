"""内核正向路径：genesis、费用、块内前序引用、逐交易 UTXO 演化、摘要。"""
from __future__ import annotations

from utxo_ledger import encoding, fab
from utxo_ledger.journal import snapshot
from utxo_ledger.kernel import Kernel
from utxo_ledger.store import SqliteStore


def test_genesis_and_transfer_utxo_evolution(ring):
    k0, k1 = ring.pub(0), ring.pub(1)
    g = fab.genesis_block([fab.issue_tx([(1000, k0)])])

    store = SqliteStore(":memory:")
    plan_g = Kernel(store).plan_block(g)
    assert len(plan_g.results) == 1
    store.apply_block(plan_g)
    g_id = encoding.txid_of(g.transactions[0])
    assert store.get_utxo(g_id, 0).amount == 1000
    assert store.utxo_count() == 1

    # 高度 1：花 1000 -> 700 + 250，费 50
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(g_id, 0)],
            [fab.make_output(700, k1), fab.make_output(250, k0)],
            fee=50,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    b1 = fab.next_block([tx], g)
    plan = Kernel(store).plan_block(b1)
    r = plan.results[0]
    assert (r.sum_in, r.sum_out, r.tx.fee) == (1000, 950, 50)
    assert plan.total_fee == 50
    store.apply_block(plan)

    # 原 UTXO 已花费；两个新 UTXO 存活；费用被销毁（本测试资产不分配给矿工）
    assert store.classify_outpoint(g_id, 0) == "spent"
    tx_id = encoding.txid_of(tx)
    assert store.get_utxo(tx_id, 0).amount == 700
    assert store.get_utxo(tx_id, 1).amount == 250
    assert store.utxo_count() == 2
    assert len(store.utxo_by_pubkey(k1)) == 1


def test_intra_block_reference_visible_to_later_tx(ring):
    """块内第二笔可引用第一笔输出；提交后中间 UTXO 已被花费。"""
    k0, k2 = ring.pub(0), ring.pub(2)
    g = fab.genesis_block([fab.issue_tx([(100, k0)])])
    g_id = encoding.txid_of(g.transactions[0])

    a = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(g_id, 0)],
            [fab.make_output(60, k2), fab.make_output(35, k0)],
            fee=5,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    a_id = encoding.txid_of(a)
    b = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(a_id, 0)],
            [fab.make_output(60, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    blk = fab.next_block([a, b], g)

    store = SqliteStore(":memory:")
    store.apply_block(Kernel(store).plan_block(g))
    plan = Kernel(store).plan_block(blk)
    assert [r.sum_in for r in plan.results] == [100, 60]
    store.apply_block(plan)
    assert store.classify_outpoint(a_id, 0) == "spent"
    assert store.get_utxo(a_id, 1).amount == 35  # a 的找零仍存活
    assert store.utxo_count() == 2  # a:1(35) + b:0(60)


def test_utxo_root_changes_on_spend_and_is_deterministic(ring):
    k0, k1 = ring.pub(0), ring.pub(1)
    g = fab.genesis_block([fab.issue_tx([(10, k0), (20, k1)])])
    s1 = SqliteStore(":memory:")
    s1.apply_block(Kernel(s1).plan_block(g))
    s2 = SqliteStore(":memory:")
    s2.apply_block(Kernel(s2).plan_block(g))
    assert s1.utxo_root() == s2.utxo_root()  # 同状态同根
    # 不同输出集合根不同
    g2 = fab.genesis_block([fab.issue_tx([(10, k0), (21, k1)])])
    s3 = SqliteStore(":memory:")
    s3.apply_block(Kernel(s3).plan_block(g2))
    assert s1.utxo_root() != s3.utxo_root()


def test_failed_plan_does_not_touch_store(base_chain):
    """规划阶段拒绝：不调用 apply，store 快照前后完全一致。"""
    import pytest

    from utxo_ledger.errors import DoubleSpendError

    store, g, b1, pubs, ring = base_chain
    before = snapshot(store, "before")
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]

    tx1 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k0"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    tx2 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k1"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    bad = fab.next_block([tx1, tx2], b1)
    with pytest.raises(DoubleSpendError) as ei:
        Kernel(store).plan_block(bad)
    assert ei.value.tx_index == 1
    after = snapshot(store, "after")
    assert {k: before[k] for k in before if k != "label"} == {
        k: after[k] for k in after if k != "label"
    }


def test_event_sink_records_per_tx_intermediate_state(ring):
    k0 = ring.pub(0)
    g = fab.genesis_block([fab.issue_tx([(42, k0)])])
    events = []
    store = SqliteStore(":memory:")
    Kernel(store, event_sink=events.append).plan_block(g)
    assert any(e["stage"] == "tx_planned" and e["sum_out"] == 42 for e in events)
    assert any(e["stage"] == "block_planned" for e in events)
