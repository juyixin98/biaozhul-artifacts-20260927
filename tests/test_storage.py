"""版本存储单元测试：显式版本化全集、删除同步、不可变快照。"""
from __future__ import annotations

import pytest

from app.storage.version_store import (
    INITIAL_VERSION,
    VersionConflictError,
    VersionNotFoundError,
    VersionStore,
)


def test_initial_version_is_empty_universe(tmp_path):
    store = VersionStore(str(tmp_path / "x.sqlite3"))
    assert store.latest_version() == INITIAL_VERSION
    assert store.universe(INITIAL_VERSION) == []
    assert store.known_terms(INITIAL_VERSION) == []


def test_commit_copies_parent_and_is_immutable(tmp_path):
    store = VersionStore(str(tmp_path / "x.sqlite3"))
    v2 = store.commit(None, adds=[(1, ["cat"]), (2, ["cat", "dog"])], message="v2")
    # 在 v2 之上建 v3：删除 doc 1
    v3 = store.commit(v2, deletes=[1], message="v3 delete 1")

    # 父版本快照不变
    assert store.universe(v2) == [1, 2]
    assert store.posting(v2, "cat") == [1, 2]
    assert store.posting(v2, "dog") == [2]

    # 新版本：删除同步全集可见性 + posting 移除
    assert store.universe(v3) == [2]
    assert store.posting(v3, "cat") == [2]
    assert store.posting(v3, "dog") == [2]


def test_re_add_deleted_doc_restores_visibility(tmp_path):
    store = VersionStore(str(tmp_path / "x.sqlite3"))
    v2 = store.commit(None, adds=[(7, ["cat"])], message="v2")
    v3 = store.commit(v2, deletes=[7], message="v3")
    assert store.universe(v3) == []
    v4 = store.commit(v3, adds=[(7, ["dog"])], message="v4 re-add")
    assert store.universe(v4) == [7]
    assert store.posting(v4, "cat") == []
    assert store.posting(v4, "dog") == [7]


def test_delete_unknown_doc_is_version_conflict(tmp_path):
    store = VersionStore(str(tmp_path / "x.sqlite3"))
    with pytest.raises(VersionConflictError):
        store.commit(None, deletes=[999], message="bad delete")


def test_unknown_version_raises_not_found(tmp_path):
    store = VersionStore(str(tmp_path / "x.sqlite3"))
    with pytest.raises(VersionNotFoundError):
        store.universe(42)


def test_posting_dedupes_repeated_terms(tmp_path):
    store = VersionStore(str(tmp_path / "x.sqlite3"))
    v = store.commit(None, adds=[(1, ["cat", "cat"]), (1, ["cat"])], message="dup")
    assert store.posting(v, "cat") == [1]


def test_events_record_delete_for_diagnostics(tmp_path):
    store = VersionStore(str(tmp_path / "x.sqlite3"))
    v2 = store.commit(None, adds=[(1, ["cat"])], message="v2")
    v3 = store.commit(v2, deletes=[1], message="v3")
    kinds = [(e["kind"], e["doc_id"]) for e in store.events(v3)]
    assert kinds == [("delete", 1)]
