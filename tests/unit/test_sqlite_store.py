"""SQLite 元数据事务单元测试：不可变、条件前进、提交原子性。"""
from __future__ import annotations

import sqlite3

import pytest

from merge3.domain.models import Branch, FieldSpec, Snapshot, TableSpec
from merge3.storage.sqlite_store import SqliteStore
from merge3.storage.run_log import utc_now_iso


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "m.db")
    spec = TableSpec("t", ["id"], [FieldSpec("id", "int64", False)])
    s.create_table(spec, utc_now_iso())
    return s


def _snap(sid, parent=None, run=None):
    return Snapshot(sid, "t", 1, f"hash-{sid}", 1, parent, run, utc_now_iso())


def test_snapshot_is_insert_only(store):
    store.insert_snapshot(_snap("s1"), [])
    # 存储层不提供更新路径；直接 SQL 篡改会被 schema 暴露，这里验证重复插入报错
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_snapshot(_snap("s1"), [])


def test_conditional_advance_rejects_stale_and_rewind(store):
    store.insert_snapshot(_snap("s1"), [])
    store.insert_snapshot(_snap("s2"), ["s1"])
    store.create_branch(Branch("main", "t", "s1"))
    with store.transaction() as conn:
        # 正确前进
        assert store.advance_branch(conn, "main", "t", "s1", "s2")
    # 旧指针再次前进失败（防覆盖/回退）
    with store.transaction() as conn:
        assert not store.advance_branch(conn, "main", "t", "s1", "s2")
        assert not store.advance_branch(conn, "main", "t", "s2", "s1")
    assert store.get_branch("main", "t").head_snapshot_id == "s2"


def test_commit_transaction_atomicity(store):
    """分支前进失败时，同事务中的快照插入必须一起回滚。"""
    store.insert_snapshot(_snap("s1"), [])
    store.create_branch(Branch("main", "t", "s1"))
    with pytest.raises(Exception):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO snapshots(snapshot_id, table_name, schema_version, "
                "content_hash, row_count, parent_snapshot_id, created_by_run_id, created_at) "
                "VALUES('sX','t',1,'h',1,NULL,NULL,?)",
                (utc_now_iso(),),
            )
            # 故意用错误旧指针触发失败并抛出
            assert store.advance_branch(conn, "main", "t", "STALE", "sX") is False
            raise RuntimeError("rollback please")
    assert store.get_snapshot("sX") is None
    assert store.get_branch("main", "t").head_snapshot_id == "s1"


def test_two_parents_persisted_and_ordered(store):
    store.insert_snapshot(_snap("s1"), [])
    store.insert_snapshot(_snap("s2"), [])
    store.insert_snapshot(_snap("m1"), [])
    store.insert_snapshot(_snap("merge"), ["m1", "s2"])
    pm = store.parent_map("t")
    assert pm["merge"] == ["m1", "s2"]  # position 0=ours, 1=theirs
