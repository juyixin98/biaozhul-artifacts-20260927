"""Version storage: immutability, activation, deterministic ordering."""
from __future__ import annotations

import pytest

from app.errors import VersionNotFoundError
from app.storage import VersionStore


def test_version_create_and_activate(tmp_path):
    store = VersionStore(tmp_path / "v.db")
    v1 = store.create_version([("abc", 1.0), ("abd", 2.0)], description="first")
    v2 = store.create_version([("xyz", 9.0)], description="second", activate=True)
    assert store.active_version() == v2
    meta = store.version_meta(v1)
    assert meta["entry_count"] == 2 and meta["description"] == "first"
    store.set_active(v1)
    assert store.active_version() == v1
    assert [v["version_id"] for v in store.list_versions()] == [v1, v2]


def test_duplicate_terms_merged_max_freq(tmp_path):
    store = VersionStore(tmp_path / "d.db")
    vid = store.create_version([("abc", 1.0), ("abc", 5.0), ("abc", 2.0)])
    rows = list(store.iter_candidates(vid, 0, 99))
    assert len(rows) == 1
    assert rows[0]["frequency"] == 5.0


def test_iter_order_is_deterministic(tmp_path):
    store = VersionStore(tmp_path / "o.db")
    vid = store.create_version([("zzz", 1), ("aaa", 1), ("mmm", 1), ("aa", 1)])
    terms = [r["term"] for r in store.iter_candidates(vid, 0, 99)]
    # (length, term) order: "aa"(2) before "aaa"(3) ...
    assert terms == ["aa", "aaa", "mmm", "zzz"]


def test_resolve_unknown_version_raises_category(tmp_path):
    store = VersionStore(tmp_path / "u.db")
    with pytest.raises(KeyError):
        store.resolve_version(12345)


def test_old_versions_remain_immutable(tmp_path):
    store = VersionStore(tmp_path / "i.db")
    v1 = store.create_version([("abc", 1.0)], description="keep")
    store.create_version([("xyz", 1.0)], description="new")
    rows = list(store.iter_candidates(v1, 0, 99))
    assert [r["term"] for r in rows] == ["abc"]
