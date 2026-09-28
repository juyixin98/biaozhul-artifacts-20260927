"""共享 pytest 夹具：独立 oracle 加载器、合成底座链、临时日志目录。"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from utxo_ledger import encoding, fab  # noqa: E402
from utxo_ledger.store import SqliteStore  # noqa: E402


@pytest.fixture(scope="session")
def oracle():
    """独立参考实现（按文件路径加载，保证不经过被测包）。"""
    path = os.path.join(ROOT, "reference", "oracle.py")
    spec = importlib.util.spec_from_file_location("independent_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ring():
    return fab.KeyRing()


@pytest.fixture
def base_chain(ring):
    """返回 (store, g, b1, pubs, ring)，store 已提交 genesis 与高度 1。"""
    k0, k1, k2 = ring.pub(0), ring.pub(1), ring.pub(2)
    g = fab.genesis_block([fab.issue_tx([(1000, k0), (500, k1)])])
    g_ids = [encoding.txid_of(t) for t in g.transactions]
    t1 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(g_ids[0], 0)],
            [fab.make_output(900, k2)],
            fee=100,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    t2 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(g_ids[0], 1)],
            [fab.make_output(500, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(1)],
    )
    b1 = fab.next_block([t1, t2], g)

    from utxo_ledger.kernel import Kernel

    store = SqliteStore(":memory:")
    for b in (g, b1):
        store.apply_block(Kernel(store).plan_block(b))
    return store, g, b1, {"k0": k0, "k1": k1, "k2": k2}, ring


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return str(d)
