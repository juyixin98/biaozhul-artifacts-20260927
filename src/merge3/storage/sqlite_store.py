"""SQLite 元数据存储，全部写操作在 IMMEDIATE 事务中完成。

不可变约束在 SQL 层与应用层双重保证：
- snapshots 只插入，不提供 UPDATE/DELETE 路径；
- 分支前进使用条件更新（只接受旧指针），防止并发覆盖、防止回退；
- 合并提交与分支指针推进在同一个事务里，要么都成功要么都回滚，
  绝不可能出现"合并快照已生成但分支历史被改坏"的中间态。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..domain.models import (
    Branch,
    MergeRun,
    MergePlan,
    MergeEntry,
    FieldDecision,
    ConflictRecord,
    Snapshot,
    TableSpec,
)

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tables (
    name TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL REFERENCES tables(name),
    schema_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    parent_snapshot_id TEXT,
    created_by_run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_table ON snapshots(table_name);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON snapshots(created_by_run_id);

-- 一个快照可有多个父（合并提交恰好两个）。
CREATE TABLE IF NOT EXISTS snapshot_parents (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    parent_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    position INTEGER NOT NULL,           -- 0=ours(开发) 1=theirs(主)
    PRIMARY KEY (snapshot_id, parent_snapshot_id)
);

CREATE TABLE IF NOT EXISTS branches (
    name TEXT NOT NULL,
    table_name TEXT NOT NULL REFERENCES tables(name),
    head_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    PRIMARY KEY (name, table_name)
);

CREATE TABLE IF NOT EXISTS merge_runs (
    run_id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL REFERENCES tables(name),
    status TEXT NOT NULL,
    ours_branch TEXT NOT NULL,
    theirs_branch TEXT NOT NULL,
    base_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    ours_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    theirs_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    plan_json TEXT NOT NULL,
    committed_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
    commit_message TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT
);
"""


class SqliteStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # ---------------------------------------------------------- 连接/事务

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """立即取写锁的事务，避免 sqlite 延迟加锁导致的写-写竞争半提交。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------- 表

    def create_table(self, spec: TableSpec, created_at: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO tables(name, spec_json, created_at) VALUES(?, ?, ?)",
                (spec.name, json.dumps(spec.to_dict(), ensure_ascii=False), created_at),
            )

    def get_table_spec(self, name: str) -> TableSpec | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT spec_json FROM tables WHERE name=?", (name,)).fetchone()
            return TableSpec.from_dict(json.loads(row["spec_json"])) if row else None
        finally:
            conn.close()

    def list_tables(self) -> list[str]:
        conn = self._connect()
        try:
            return [r["name"] for r in conn.execute("SELECT name FROM tables ORDER BY name")]
        finally:
            conn.close()

    # ---------------------------------------------------------- 快照（只增）

    def insert_snapshot(self, snap: Snapshot, parents: list[str]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO snapshots(snapshot_id, table_name, schema_version, content_hash, "
                "row_count, parent_snapshot_id, created_by_run_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snap.snapshot_id, snap.table, snap.schema_version, snap.content_hash,
                    snap.row_count, snap.parent_snapshot_id, snap.created_by_run_id,
                    snap.created_at,
                ),
            )
            for pos, pid in enumerate(parents):
                conn.execute(
                    "INSERT INTO snapshot_parents(snapshot_id, parent_snapshot_id, position) "
                    "VALUES(?, ?, ?)",
                    (snap.snapshot_id, pid, pos),
                )

    def get_snapshot(self, snapshot_id: str) -> Snapshot | None:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            return _snapshot_from_row(r) if r else None
        finally:
            conn.close()

    def list_snapshots(self, table: str) -> list[Snapshot]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE table_name=? ORDER BY created_at, snapshot_id",
                (table,),
            ).fetchall()
            return [_snapshot_from_row(r) for r in rows]
        finally:
            conn.close()

    def parent_map(self, table: str) -> dict[str, list[str]]:
        """返回 {snapshot_id: [父...按 position]}，用于 LCA/血缘。"""
        conn = self._connect()
        try:
            ids = {
                r["snapshot_id"]
                for r in conn.execute(
                    "SELECT snapshot_id FROM snapshots WHERE table_name=?", (table,)
                )
            }
            pm: dict[str, list[str]] = {sid: [] for sid in ids}
            rows = conn.execute(
                "SELECT sp.snapshot_id AS sid, sp.parent_snapshot_id AS pid, sp.position AS pos "
                "FROM snapshot_parents sp JOIN snapshots s ON s.snapshot_id=sp.snapshot_id "
                "WHERE s.table_name=? ORDER BY sp.position",
                (table,),
            ).fetchall()
            for r in rows:
                pm[r["sid"]].append(r["pid"])
            return pm
        finally:
            conn.close()

    # ---------------------------------------------------------- 分支

    def create_branch(self, branch: Branch) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO branches(name, table_name, head_snapshot_id) VALUES(?, ?, ?)",
                (branch.name, branch.table, branch.head_snapshot_id),
            )

    def get_branch(self, name: str, table: str) -> Branch | None:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT name, table_name, head_snapshot_id FROM branches "
                "WHERE name=? AND table_name=?",
                (name, table),
            ).fetchone()
            return Branch(r["name"], r["table_name"], r["head_snapshot_id"]) if r else None
        finally:
            conn.close()

    def list_branches(self, table: str) -> list[Branch]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name, table_name, head_snapshot_id FROM branches "
                "WHERE table_name=? ORDER BY name",
                (table,),
            ).fetchall()
            return [Branch(r["name"], r["table_name"], r["head_snapshot_id"]) for r in rows]
        finally:
            conn.close()

    def advance_branch(
        self, conn: sqlite3.Connection, name: str, table: str,
        old_head: str, new_head: str,
    ) -> bool:
        """条件前进（同事务内调用）。

        两个条件同时满足才更新：
        1) 当前指针 == old_head（防止并发覆盖）；
        2) new_head == old_head（幂等空前进）或 old_head 是 new_head 的祖先
           （沿 snapshot_parents 可达）——防止分支指针回退、防止重新导入覆盖历史。
        """
        if new_head != old_head:
            row = conn.execute(
                """
                WITH RECURSIVE anc(a) AS (
                    SELECT ?
                    UNION
                    SELECT sp.parent_snapshot_id
                      FROM snapshot_parents sp JOIN anc ON sp.snapshot_id = anc.a
                )
                SELECT COUNT(*) AS c FROM anc WHERE a = ?
                """,
                (new_head, old_head),
            ).fetchone()
            if row["c"] == 0:
                return False
        cur = conn.execute(
            "UPDATE branches SET head_snapshot_id=? "
            "WHERE name=? AND table_name=? AND head_snapshot_id=?",
            (new_head, name, table, old_head),
        )
        return cur.rowcount == 1

    # ---------------------------------------------------------- 合并运行

    def insert_merge_run(self, run: MergeRun) -> None:
        with self.transaction() as conn:
            self._upsert_run(conn, run)

    def save_merge_run(self, run: MergeRun) -> None:
        with self.transaction() as conn:
            self._upsert_run(conn, run)

    def _upsert_run(self, conn: sqlite3.Connection, run: MergeRun) -> None:
        conn.execute(
            "INSERT INTO merge_runs(run_id, table_name, status, ours_branch, theirs_branch, "
            "base_snapshot_id, ours_snapshot_id, theirs_snapshot_id, plan_json, "
            "committed_snapshot_id, commit_message, created_at, committed_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "status=excluded.status, plan_json=excluded.plan_json, "
            "committed_snapshot_id=excluded.committed_snapshot_id, "
            "commit_message=excluded.commit_message, committed_at=excluded.committed_at",
            (
                run.run_id, run.table, run.status, run.ours_branch, run.theirs_branch,
                run.base_snapshot_id, run.ours_snapshot_id, run.theirs_snapshot_id,
                json.dumps(plan_to_dict(run.plan), ensure_ascii=False),
                run.committed_snapshot_id, run.commit_message, run.created_at,
                run.committed_at,
            ),
        )

    def get_merge_run(self, run_id: str) -> MergeRun | None:
        conn = self._connect()
        try:
            r = conn.execute("SELECT * FROM merge_runs WHERE run_id=?", (run_id,)).fetchone()
            return _run_from_row(r) if r else None
        finally:
            conn.close()

    def list_merge_runs(self, table: str) -> list[MergeRun]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM merge_runs WHERE table_name=? ORDER BY created_at", (table,)
            ).fetchall()
            return [_run_from_row(r) for r in rows]
        finally:
            conn.close()


# ---------------------------------------------------------------- 序列化

def plan_to_dict(plan: MergePlan) -> dict[str, Any]:
    return plan.to_dict()


def plan_from_dict(d: dict[str, Any]) -> MergePlan:
    entries: dict[str, MergeEntry] = {}
    for k, ed in d["entries"].items():
        fields = {
            name: FieldDecision(**fd) for name, fd in ed.get("fields", {}).items()
        }
        entries[k] = MergeEntry(
            key=ed["key"],
            classification=ed["classification"],
            conflict=ed["conflict"],
            reason=ed["reason"],
            merged=ed["merged"],
            deleted=ed["deleted"],
            fields=fields,
            base_row=ed.get("base_row"),
            ours_row=ed.get("ours_row"),
            theirs_row=ed.get("theirs_row"),
            conflicting_fields=ed.get("conflicting_fields", []),
        )
    conflicts = {k: ConflictRecord(**cd) for k, cd in d["conflicts"].items()}
    return MergePlan(
        table=d["table"],
        primary_key=d["primary_key"],
        base_snapshot_id=d["base_snapshot_id"],
        ours_snapshot_id=d["ours_snapshot_id"],
        theirs_snapshot_id=d["theirs_snapshot_id"],
        entries=entries,
        conflicts=conflicts,
    )


def _snapshot_from_row(r: sqlite3.Row) -> Snapshot:
    return Snapshot(
        snapshot_id=r["snapshot_id"],
        table=r["table_name"],
        schema_version=r["schema_version"],
        content_hash=r["content_hash"],
        row_count=r["row_count"],
        parent_snapshot_id=r["parent_snapshot_id"],
        created_by_run_id=r["created_by_run_id"],
        created_at=r["created_at"],
    )


def _run_from_row(r: sqlite3.Row) -> MergeRun:
    return MergeRun(
        run_id=r["run_id"],
        table=r["table_name"],
        status=r["status"],
        base_snapshot_id=r["base_snapshot_id"],
        ours_snapshot_id=r["ours_snapshot_id"],
        theirs_snapshot_id=r["theirs_snapshot_id"],
        ours_branch=r["ours_branch"],
        theirs_branch=r["theirs_branch"],
        plan=plan_from_dict(json.loads(r["plan_json"])),
        created_at=r["created_at"],
        committed_snapshot_id=r["committed_snapshot_id"],
        commit_message=r["commit_message"],
        committed_at=r["committed_at"],
    )
