"""共享测试夹具。

注意：所有“期望动作集合”均在测试中手工推导（带注释说明推导过程），
不由被测内核生成；随机化用例的交叉核对见 oracle.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# 显式确保 src 在路径上（pyproject pythonpath 之外的直接运行场景）
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from merge_engine import MergeEngine, MergeRequest  # noqa: E402
from merge_engine import store  # noqa: E402


@pytest.fixture
def workdir(tmp_path):
    db = tmp_path / "merge.db"
    journal = tmp_path / "journal"
    return tmp_path, db, journal


@pytest.fixture
def engine(workdir):
    _, db, journal = workdir
    return MergeEngine(db, journal)


def make_engine(tmp_path):
    return MergeEngine(tmp_path / "merge.db", tmp_path / "journal")


def seed(tmp_path, table, columns, rows):
    """绕过引擎直接构造目标初始状态（可制造重复键等脏状态）。"""
    eng = MergeEngine(tmp_path / "merge.db", tmp_path / "journal")
    conn = store.connect(eng.db_path)
    try:
        store.ensure_meta(conn)
        store.seed_target(conn, table, columns, rows)
        conn.commit()
    finally:
        conn.close()
    return eng


def request_for(records, config, *, dry_run=False, fault_point=None, fmt="records",
                **extra):
    source = {"format": fmt, "records": records} if fmt == "records" else extra["source"]
    return MergeRequest(source=source, config=config, dry_run=dry_run,
                        fault_point=fault_point)


def write_parquet(tmp_path, name, records: list[dict]):
    path = tmp_path / name
    table = pa.Table.from_pylist(records)
    pq.write_table(table, str(path))
    return path


# 常用配置构造器（测试里显式给出，避免“核心生成答案”）
def cfg(table="accounts", keys=("k1", "k2"), *, null="SQL", update=None,
        insert=None, delete_unmatched=False, delete=None, **limits):
    c = {
        "target_table": table,
        "key_columns": list(keys),
        "null_equality": null,
    }
    if update is not None:
        c["update_when"] = update
    if insert is not None:
        c["insert_when"] = insert
    if delete is not None:
        c["delete_when"] = delete
    if delete_unmatched:
        c["delete_unmatched"] = True
    c.update(limits)
    return c


def actions_of(result):
    assert result.plan is not None
    return [(a.type.value, tuple(a.key), a.reason) for a in result.plan.actions]


def write_actions_of(result):
    assert result.plan is not None
    return [(a.type.value, tuple(a.key)) for a in result.plan.write_actions()]


@pytest.fixture
def make_parquet(tmp_path):
    def _make(name, records):
        return write_parquet(tmp_path, name, records)
    return _make
