"""元数据事务层：SQLite 持久化。

表：
- tables:        表定义（列模式、分区列）
- snapshots:     不可变快照（id/parent_id/commit_kind/...），id=0 为虚拟空快照
- manifest_files:每个快照的数据文件清单
- commit_log:    每次提交请求的结果（接受/拒绝/失败），request_id 唯一，支持幂等重放
- staged_files:  暂存台账（ready/consumed/failed）
- cleanup_ledger:文件清理记录（独立记录每个失败/已发布/孤立文件的处置）

并发模型：单进程内一个长连接 + 一把可重入锁（RLock），**所有**对连接的访问
都在锁内，保证同一时刻只有一个线程使用连接；提交事务在同一把锁内执行
BEGIN IMMEDIATE..COMMIT，读方法在事务期间自然排队。锁可重入，事务内调用其它
元数据方法安全。冲突裁决本身是纯逻辑（见 kernel），不依赖锁顺序。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS tables (
    name             TEXT PRIMARY KEY,
    columns_json     TEXT NOT NULL,
    partition_column TEXT NOT NULL,
    created_at       REAL NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name      TEXT NOT NULL REFERENCES tables(name),
    parent_id       INTEGER NOT NULL,
    commit_kind     TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    added_files     INTEGER NOT NULL,
    removed_files   INTEGER NOT NULL,
    total_files     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_table ON snapshots(table_name, id);

CREATE TABLE IF NOT EXISTS manifest_files (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    table_name  TEXT NOT NULL,
    path        TEXT NOT NULL,
    partition   TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    row_count   INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, path)
);
CREATE INDEX IF NOT EXISTS ix_manifest_table ON manifest_files(table_name, path);

CREATE TABLE IF NOT EXISTS commit_log (
    request_id      TEXT PRIMARY KEY,
    table_name      TEXT NOT NULL,
    commit_kind     TEXT NOT NULL,
    base_snapshot_id INTEGER NOT NULL,
    status          TEXT NOT NULL,            -- ACCEPTED / REJECTED / PUBLISH_FAILED
    reason_code     TEXT,
    snapshot_id     INTEGER,                 -- 接受时产生的快照 id
    detail_json     TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_commit_log_table ON commit_log(table_name, created_at);

CREATE TABLE IF NOT EXISTS staged_files (
    request_id  TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    path        TEXT NOT NULL,               -- 相对仓库根
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    row_count   INTEGER NOT NULL,
    partition   TEXT NOT NULL,
    status      TEXT NOT NULL,               -- ready / consumed / failed
    PRIMARY KEY (request_id, logical_name)
);
CREATE INDEX IF NOT EXISTS ix_staged_status ON staged_files(status, request_id);

CREATE TABLE IF NOT EXISTS cleanup_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,           -- stage_failed / stage_published / orphan_data / orphan_staging
    request_id      TEXT,
    table_name      TEXT,
    src_path        TEXT NOT NULL,
    dest_path       TEXT,                    -- 隔离后的位置；已删除为 NULL
    reason_code     TEXT NOT NULL,
    status          TEXT NOT NULL,           -- quarantined / deleted
    detail_json     TEXT NOT NULL,
    created_at      REAL NOT NULL
);
"""


@dataclass(frozen=True)
class TableDef:
    name: str
    columns: tuple[dict[str, str], ...]
    partition_column: str
    created_at: float
    version: int = 1


@dataclass(frozen=True)
class SnapshotRow:
    id: int
    table_name: str
    parent_id: int
    commit_kind: str
    request_id: str
    created_at: float
    added_files: int
    removed_files: int
    total_files: int


@dataclass(frozen=True)
class CommitLogRow:
    request_id: str
    table_name: str
    commit_kind: str
    base_snapshot_id: int
    status: str
    reason_code: str | None
    snapshot_id: int | None
    detail: dict[str, Any]
    created_at: float


class Catalog:
    """元数据访问对象。所有连接访问均在 RLock 内串行化。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_DDL)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- 表定义 ----
    def create_table(
        self, name: str, columns: list[dict[str, str]], partition_column: str, now: float
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tables(name, columns_json, partition_column, created_at) "
                "VALUES (?,?,?,?)",
                (name, json.dumps(columns), partition_column, now),
            )

    def get_table(self, name: str) -> TableDef | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tables WHERE name=?", (name,)
            ).fetchone()
        if row is None:
            return None
        return TableDef(
            name=row["name"],
            columns=tuple(json.loads(row["columns_json"])),
            partition_column=row["partition_column"],
            created_at=row["created_at"],
            version=row["version"],
        )

    def list_tables(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT name FROM tables ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    # ---- 快照与清单 ----
    def head_snapshot_id(self, table: str) -> int:
        """返回当前表头快照；空表返回虚拟 id=0。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(id) AS m FROM snapshots WHERE table_name=?", (table,)
            ).fetchone()
        return int(row["m"] or 0)

    def get_snapshot(self, table: str, snapshot_id: int) -> SnapshotRow | None:
        if snapshot_id == 0:
            return SnapshotRow(0, table, 0, "EMPTY", "-", 0.0, 0, 0, 0)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE table_name=? AND id=?", (table, snapshot_id)
            ).fetchone()
        return _snapshot_row(row) if row else None

    def list_snapshots(self, table: str) -> list[SnapshotRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM snapshots WHERE table_name=? ORDER BY id", (table,)
            ).fetchall()
        return [_snapshot_row(r) for r in rows]

    def manifest_paths(self, table: str, snapshot_id: int) -> set[str]:
        if snapshot_id == 0:
            return set()
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM manifest_files WHERE table_name=? AND snapshot_id=?",
                (table, snapshot_id),
            ).fetchall()
        return {r["path"] for r in rows}

    def manifest_rows(self, table: str, snapshot_id: int) -> tuple[dict[str, Any], ...]:
        if snapshot_id == 0:
            return ()
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, partition, sha256, size_bytes, row_count "
                "FROM manifest_files WHERE table_name=? AND snapshot_id=?",
                (table, snapshot_id),
            ).fetchall()
        return tuple(dict(r) for r in rows)

    def all_data_paths(self, table: str) -> set[str]:
        """任意快照清单中出现过的全部数据文件（用于孤立判定）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT path FROM manifest_files WHERE table_name=?", (table,)
            ).fetchall()
        return {r["path"] for r in rows}

    def insert_snapshot(
        self,
        cur: sqlite3.Cursor,
        table: str,
        parent_id: int,
        commit_kind: str,
        request_id: str,
        now: float,
        added: int,
        removed: int,
        total: int,
        files: Iterable[dict[str, Any]],
    ) -> int:
        """在给定事务游标内插入快照与清单，返回新快照 id。调用方须持锁。"""
        cur.execute(
            "INSERT INTO snapshots(table_name, parent_id, commit_kind, request_id, "
            "created_at, added_files, removed_files, total_files) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (table, parent_id, commit_kind, request_id, now, added, removed, total),
        )
        snapshot_id = int(cur.lastrowid)
        cur.executemany(
            "INSERT INTO manifest_files(snapshot_id, table_name, path, partition, "
            "sha256, size_bytes, row_count) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    snapshot_id,
                    table,
                    f["path"],
                    f["partition"],
                    f["sha256"],
                    f["size_bytes"],
                    f["row_count"],
                )
                for f in files
            ],
        )
        return snapshot_id

    # ---- 提交日志（幂等）----
    def get_commit(self, request_id: str) -> CommitLogRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM commit_log WHERE request_id=?", (request_id,)
            ).fetchone()
        return _commit_row(row) if row else None

    def get_commit_for_table(self, request_id: str, table: str) -> CommitLogRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM commit_log WHERE request_id=? AND table_name=?",
                (request_id, table),
            ).fetchone()
        return _commit_row(row) if row else None

    def commits_between(
        self, table: str, base_snapshot_id: int, head_snapshot_id: int
    ) -> list[CommitLogRow]:
        """基线快照之后产生的成功提交（按快照 id 顺序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM commit_log WHERE table_name=? AND status='ACCEPTED' "
                "AND snapshot_id > ? AND snapshot_id <= ? ORDER BY snapshot_id",
                (table, base_snapshot_id, head_snapshot_id),
            ).fetchall()
        return [_commit_row(r) for r in rows]

    def insert_commit(
        self,
        cur: sqlite3.Cursor,
        request_id: str,
        table: str,
        commit_kind: str,
        base_snapshot_id: int,
        status: str,
        reason_code: str | None,
        snapshot_id: int | None,
        detail: dict[str, Any],
        now: float,
    ) -> None:
        """在给定事务游标内写提交日志。调用方须持锁。"""
        cur.execute(
            "INSERT INTO commit_log(request_id, table_name, commit_kind, "
            "base_snapshot_id, status, reason_code, snapshot_id, detail_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                table,
                commit_kind,
                base_snapshot_id,
                status,
                reason_code,
                snapshot_id,
                json.dumps(detail, ensure_ascii=False, default=str),
                now,
            ),
        )

    # ---- 暂存台账 ----
    def upsert_staged_ready(
        self, request_id: str, table: str, entries: list[dict[str, Any]]
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for e in entries:
                    self._conn.execute(
                        "INSERT INTO staged_files(request_id, logical_name, table_name, "
                        "path, sha256, size_bytes, row_count, partition, status) "
                        "VALUES (?,?,?,?,?,?,?,?, 'ready') "
                        "ON CONFLICT(request_id, logical_name) DO UPDATE SET "
                        "table_name=excluded.table_name, path=excluded.path, "
                        "sha256=excluded.sha256, size_bytes=excluded.size_bytes, "
                        "row_count=excluded.row_count, partition=excluded.partition, "
                        "status='ready'",
                        (
                            request_id,
                            e["logical_name"],
                            table,
                            e["path"],
                            e["sha256"],
                            e["size_bytes"],
                            e["row_count"],
                            e["partition"],
                        ),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def mark_staged_status(
        self,
        cur: sqlite3.Cursor,
        request_id: str,
        logical_names: Iterable[str],
        status: str,
    ) -> None:
        """调用方须持锁（事务内）。"""
        cur.executemany(
            "UPDATE staged_files SET status=? WHERE request_id=? AND logical_name=?",
            [(status, request_id, n) for n in logical_names],
        )

    def get_staged_ready(self, request_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM staged_files WHERE request_id=? AND status='ready'",
                    (request_id,),
                ).fetchall()
            )

    def get_staged_any(self, request_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM staged_files WHERE request_id=? ORDER BY logical_name",
                    (request_id,),
                ).fetchall()
            )

    def delete_staged(self, cur: sqlite3.Cursor, request_id: str) -> None:
        cur.execute("DELETE FROM staged_files WHERE request_id=?", (request_id,))

    # ---- 清理台账（每个失败文件独立记录）----
    def add_cleanup(
        self,
        kind: str,
        src_path: str,
        reason_code: str,
        status: str,
        now: float,
        request_id: str | None = None,
        table_name: str | None = None,
        dest_path: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cleanup_ledger(kind, request_id, table_name, src_path, "
                "dest_path, reason_code, status, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    request_id,
                    table_name,
                    src_path,
                    dest_path,
                    reason_code,
                    status,
                    json.dumps(detail or {}, ensure_ascii=False, default=str),
                    now,
                ),
            )
            return int(cur.lastrowid)

    def list_cleanup(self, request_id: str | None = None, kind: str | None = None):
        sql = "SELECT * FROM cleanup_ledger WHERE 1=1"
        params: list[Any] = []
        if request_id:
            sql += " AND request_id=?"
            params.append(request_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def _snapshot_row(r: sqlite3.Row) -> SnapshotRow:
    return SnapshotRow(
        id=r["id"],
        table_name=r["table_name"],
        parent_id=r["parent_id"],
        commit_kind=r["commit_kind"],
        request_id=r["request_id"],
        created_at=r["created_at"],
        added_files=r["added_files"],
        removed_files=r["removed_files"],
        total_files=r["total_files"],
    )


def _commit_row(r: sqlite3.Row) -> CommitLogRow:
    return CommitLogRow(
        request_id=r["request_id"],
        table_name=r["table_name"],
        commit_kind=r["commit_kind"],
        base_snapshot_id=r["base_snapshot_id"],
        status=r["status"],
        reason_code=r["reason_code"],
        snapshot_id=r["snapshot_id"],
        detail=json.loads(r["detail_json"]),
        created_at=r["created_at"],
    )
