"""pytest 共享夹具。

测试数据库一律使用 tmp_path 下的临时 SQLite，与开发库隔离；
fixture.json 中的参考答案由独立集合代数生成（见 scripts/generate_fixture.py），
不是由被测核心产出的。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from app.query.engine import Engine  # noqa: E402
from app.storage.version_store import INITIAL_VERSION, VersionStore  # noqa: E402

FIXTURE_PATH = os.path.join(ROOT, "samples", "fixture.json")


@pytest.fixture(scope="session")
def fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def seed_from_fixture(db_path: str, fixture: dict) -> VersionStore:
    """按夹具建立 v1（空）/ v2（完整）/ v3（删除后）三个版本。"""
    store = VersionStore(db_path)
    store.reset()

    v2 = fixture["versions"][1]
    adds = [
        (doc_id, [term for term, docs in v2["terms"].items() if doc_id in docs])
        for doc_id in v2["universe"]
    ]
    vid2 = store.commit(parent_id=INITIAL_VERSION, adds=adds, message="测试：完整语料")
    v3 = fixture["versions"][2]
    vid3 = store.commit(
        parent_id=vid2, adds=[], deletes=v3["deletes"], message="测试：删除文档"
    )
    assert (vid2, vid3) == (2, 3)
    return store


@pytest.fixture()
def store(tmp_path, fixture):
    db_path = str(tmp_path / "test_index.sqlite3")
    s = seed_from_fixture(db_path, fixture)
    yield s
    s.close()


@pytest.fixture()
def engine(store, fixture):
    return Engine(store, block_size=fixture["block_size"])


@pytest.fixture()
def empty_store(tmp_path):
    """只有初始空版本 v1（空全集）的存储。"""
    s = VersionStore(str(tmp_path / "empty.sqlite3"))
    yield s
    s.close()
