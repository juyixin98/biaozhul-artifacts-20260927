"""测试公共夹具。

顶层 conftest.py 同时保证项目根目录在 sys.path 中（可 import app.*）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.storage.version_store import VersionStore  # noqa: E402


@pytest.fixture()
def block_size() -> int:
    return 8


@pytest.fixture()
def store(tmp_path, block_size):
    s = VersionStore(str(tmp_path / "test.db"), block_size=block_size)
    yield s
    s.close()


@pytest.fixture()
def seeded_store(store):
    """对应 sample_data 的 3 版本语料：v1 8 篇，v2 加 9 删 8。"""
    store.commit(
        adds={
            1: "alpha beta gamma 索引 算法",
            2: "alpha beta delta 索引 版本",
            3: "alpha rareword 版本 存储",
            4: "beta gamma 算法 持久化",
            5: "gamma delta 索引 查询",
            6: "common 全集 持久化 查询",
            7: "common 全集 版本 算法",
            8: "common 全集 存储 删除测试",
        }
    )
    store.commit(adds={9: "alpha epsilon 查询 新版本"}, deletes=[8])
    return store
