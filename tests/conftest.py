"""pytest 夹具与**独立 oracle**。

oracle 刻意不调用被测服务的任何业务方法：
- 用独立 sqlite3 连接读取快照/清单表；
- 用 PyArrow 直接扫描清单中列出的物理 Parquet 文件；
- 期望行集由测试自己持有的夹具输入 + 观察到的接受/拒绝结果独立计算。

这样“参考答案”不是由被测核心自己生成的。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from lake_txn.api import create_app
from lake_txn.config import Settings
from lake_txn.service import LakeService

TABLE = "orders"
COLUMNS = [
    {"name": "order_id", "type": "int64"},
    {"name": "region", "type": "string"},
    {"name": "amount", "type": "float64"},
]
PARTITION_COL = "region"


def make_settings(tmp_path: Path, inbound: tuple[Path, ...] = ()) -> Settings:
    root = tmp_path / "warehouse"
    return Settings(
        root=root,
        db_path=root / "metadata.sqlite3",
        staging_dir=root / "staging",
        quarantine_dir=root / "quarantine",
        orphan_grace_seconds=0,
        redact_fields=frozenset({"ssn", "email", "token"}),
        allowed_inbound_dirs=tuple(inbound),
    )


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture()
def service(settings: Settings) -> LakeService:
    svc = LakeService(settings)
    svc.create_table(TABLE, COLUMNS, PARTITION_COL)
    return svc


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        c.post(
            "/v1/tables",
            json={"table": TABLE, "columns": COLUMNS, "partition_column": PARTITION_COL},
        )
        yield c


# ---------------- 独立 oracle（不经过被测服务代码） ----------------
class IndependentOracle:
    def __init__(self, settings: Settings):
        self.root = settings.root
        self.db_path = settings.db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def head_snapshot(self, table: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(id) m FROM snapshots WHERE table_name=?", (table,)
            ).fetchone()
        return int(row["m"] or 0)

    def manifest(self, table: str, snapshot_id: int) -> list[dict]:
        if snapshot_id == 0:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT path, partition, sha256, size_bytes, row_count "
                "FROM manifest_files WHERE table_name=? AND snapshot_id=? ORDER BY path",
                (table, snapshot_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def snapshot_chain(self, table: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, parent_id, commit_kind, request_id, added_files, "
                "removed_files, total_files FROM snapshots WHERE table_name=? ORDER BY id",
                (table,),
            ).fetchall()
        return [dict(r) for r in rows]

    def rows_of(self, table: str, snapshot_id: int) -> list[dict]:
        """直接用 PyArrow 物理扫描快照清单中的每个文件。"""
        rows: list[dict] = []
        for m in self.manifest(table, snapshot_id):
            rows.extend(pq.read_table(self.root / m["path"]).to_pylist())
        return rows

    def rows_by_partition(self, table: str, snapshot_id: int) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for row in self.rows_of(table, snapshot_id):
            out.setdefault(row[PARTITION_COL], []).append(row)
        return out

    def file_sha256_set(self, table: str, snapshot_id: int) -> set[str]:
        return {m["sha256"] for m in self.manifest(table, snapshot_id)}

    def commit_log(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT request_id, status, reason_code, snapshot_id, base_snapshot_id "
                "FROM commit_log ORDER BY rowid"
            ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_records(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT kind, request_id, src_path, dest_path, reason_code, status "
                "FROM cleanup_ledger ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


@pytest.fixture()
def oracle(settings: Settings) -> IndependentOracle:
    return IndependentOracle(settings)


# ---------------- 测试用便捷辅助 ----------------
def stage_inline(
    svc: LakeService,
    request_id: str,
    partition: str,
    order_ids: list[int],
    table: str = TABLE,
    amount: float = 10.0,
):
    """直接走服务暂存（inline 由服务写 Parquet）。"""
    from lake_txn.service import StageFileInput

    records = [{"order_id": i, "region": partition, "amount": amount} for i in order_ids]
    return svc.stage_files(
        table,
        request_id,
        [StageFileInput(logical_name=f"{request_id}-0", mode="inline", records=records)],
    )


def commit_append(svc: LakeService, request_id: str, base: int) -> dict:
    return svc.commit(TABLE, request_id, "APPEND", base, [f"{request_id}-0"])


def make_old(path: Path) -> None:
    """把文件/目录 mtime 调到很久以前，绕过清扫宽限期。"""
    old = 1_000_000_000  # 2001-09-09
    os.utime(path, (old, old))


def thread_id() -> str:
    return f"t{threading.get_ident() % 100000}"
