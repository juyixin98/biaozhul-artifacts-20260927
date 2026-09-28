"""存储边界测试：原子提交、回滚后状态、WITHOUT ROWID 约束、审计表分类。"""
from __future__ import annotations

import pytest

from utxo_ledger import encoding, fab
from utxo_ledger.journal import snapshot
from utxo_ledger.kernel import Kernel
from utxo_ledger.store import SqliteStore


def test_persistent_db_reopens_and_replays(tmp_path, ring):
    path = str(tmp_path / "chain.db")
    k0 = ring.pub(0)
    g = fab.genesis_block([fab.issue_tx([(100, k0)])])
    s = SqliteStore(path)
    s.apply_block(Kernel(s).plan_block(g))
    root = s.utxo_root()
    tip = s.tip()
    s.close()

    s2 = SqliteStore(path)
    assert s2.tip() == tip
    assert s2.utxo_root() == root
    s2.close()


def test_spent_audit_distinguishes_spent_from_unknown(base_chain):
    store, g, b1, _pubs, _ring = base_chain
    g_ids = [encoding.txid_of(t) for t in g.transactions]
    assert store.classify_outpoint(g_ids[0], 0) == "spent"  # b1 花掉
    assert store.classify_outpoint(b"\x99" * 32, 0) == "unknown"
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    assert store.classify_outpoint(b1_ids[0], 0) == "unspent"


def test_apply_block_is_single_transaction_on_plan(base_chain):
    """apply_block 只消费已通过内核的 plan；异常路径回滚不留半成品。"""
    store, g, b1, _pubs, _ring = base_chain
    before = snapshot(store, "before")
    # 直接给 apply_block 喂一个结构损坏的 plan（模拟实现缺陷），
    # 存储层兜底必须抛错且状态不变。
    class BrokenResult:
        txid = b"\x77" * 32
        committed_inputs = (
            type("U", (), {"txid": b"\x77" * 32, "vout": 0}),
        )
        new_outputs = ()
        tx = type("T", (), {"fee": 0})()

    class BrokenPlan:
        block = encoding.Block(
            header=encoding.BlockHeader(
                version=1,
                height=2,
                prev_hash=encoding.block_id_of(b1),
                timestamp=fab.FIXTURE_TIMESTAMP,
                tx_root=encoding.ZERO_HASH,
                witness_root=encoding.ZERO_HASH,
            ),
            transactions=(),
        )
        block_id = b"\x77" * 32
        results = (BrokenResult(),)

    from utxo_ledger.errors import BlockConflictError

    with pytest.raises(BlockConflictError):
        store.apply_block(BrokenPlan())
    after = snapshot(store, "after")
    assert {k: before[k] for k in before if k != "label"} == {
        k: after[k] for k in after if k != "label"
    }


def test_duplicate_tx_rejected_by_storage_guard(base_chain):
    """存储层兜底：同 txid 再次提交（绕过内核）抛冲突。"""
    store, g, b1, _pubs, _ring = base_chain
    plan = Kernel(store).plan_block(
        fab.next_block(
            [
                fab.sign_tx(
                    fab.unsigned_tx(
                        [
                            encoding.Outpoint(
                                encoding.txid_of(b1.transactions[0]), 0
                            )
                        ],
                        [fab.make_output(900, _pubs["k0"])],
                        fee=0,
                    ),
                    owner_privkeys=[_ring.priv(2)],
                )
            ],
            b1,
        )
    )
    store.apply_block(plan)
    # 再次 apply 同一 plan（高度相同）必须冲突
    with pytest.raises(Exception):  # noqa: B017, BLE001
        store.apply_block(plan)
