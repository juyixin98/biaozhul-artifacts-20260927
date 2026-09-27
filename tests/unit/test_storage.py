"""版本存储测试：提交、版本化全集、删除语义、块持久化与 trace。"""
from __future__ import annotations

import pytest

from app.storage.version_store import StorageError, VersionStore


def test_version0_is_empty_universe(store):
    assert store.latest_version() == 0
    assert list(store.universe(0).ids) == []
    assert store.versions()[0] == {"version": 0, "event_count": 0, "universe_size": 0}


def test_commit_advances_version_and_materializes_universe(store):
    v1 = store.commit(adds={1: "a b", 2: "a c"})
    assert v1 == 1
    assert set(store.universe(1).ids) == {1, 2}
    v2 = store.commit(adds={3: "b c"})
    assert v2 == 2
    assert set(store.universe(2).ids) == {1, 2, 3}
    # 旧版本快照不可变
    assert set(store.universe(1).ids) == {1, 2}


def test_delete_syncs_universe_but_keeps_postings_and_blocks(store):
    store.commit(adds={1: "a b", 2: "a c", 3: "b c"})
    before_a = list(store.posting("a").ids)
    store.commit(deletes=[2])
    # 全集可见性：2 消失
    assert set(store.universe(2).ids) == {1, 3}
    # 关键不变量：posting 列表与跳跃块不重写，仍包含被删除 ID
    assert list(store.posting("a").ids) == before_a == [1, 2]
    assert store.posting("a").block_uppers  # 块索引依然存在
    # 文档行保留但标记不可见
    assert store.document(2)["active"] == 0


def test_re_add_restores_visibility_without_duplicate(store):
    store.commit(adds={1: "a b", 2: "a c"})
    store.commit(deletes=[2])
    store.commit(adds={2: "a c d"})  # 重新加入
    assert set(store.universe(3).ids) == {1, 2}
    assert list(store.posting("a").ids) == [1, 2]  # 无重复
    assert list(store.posting("d").ids) == [2]


def test_posting_blocks_roundtrip_with_correct_uppers(store):
    store.commit(adds={i: "x" for i in range(1, 21)})
    p = store.posting("x")
    assert len(p) == 20
    assert [b.upper for b in p.blocks] == [8, 16, 20]
    # 再开一个存储实例验证真正持久化
    store.close()
    reopened = VersionStore(store.db_path, block_size=store.block_size)
    assert list(reopened.posting("x").ids) == list(range(1, 21))
    assert reopened.latest_version() == 1


def test_resolve_version_none_uses_latest_missing_raises(store):
    store.commit(adds={1: "a"})
    assert store.resolve_version(None) == 1
    assert store.resolve_version(1) == 1
    with pytest.raises(StorageError, match="版本 9 不存在"):
        store.resolve_version(9)


def test_empty_commit_rejected(store):
    with pytest.raises(StorageError, match="空提交"):
        store.commit()


def test_delete_non_existent_is_idempotent(store):
    store.commit(adds={1: "a"})
    v = store.commit(deletes=[999])
    assert set(store.universe(v).ids) == {1}


def test_term_listing_and_prefix(store):
    store.commit(adds={1: "apple apricot banana", 2: "avocado"})
    assert store.list_terms(prefix="ap") == ["apple", "apricot"]
    assert "banana" in store.list_terms()


def test_trace_persisted_and_retrievable(store):
    store.save_trace(
        {
            "request_id": "rid-123",
            "started_at": 1.0,
            "finished_at": 2.0,
            "version": 1,
            "expression": "a",
            "status": "ok",
            "result_count": 0,
            "stats": {"blocks_skipped": 3},
            "steps": [{"op": "term_load"}],
        }
    )
    t = store.get_trace("rid-123")
    assert t["stats"] == {"blocks_skipped": 3}
    assert t["steps"] == [{"op": "term_load"}]
    assert store.get_trace("missing") is None
    recent = store.recent_traces()
    assert recent[0]["request_id"] == "rid-123"
