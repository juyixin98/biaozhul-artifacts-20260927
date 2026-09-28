"""SQLite 元数据事务层。

存储：表定义、文件版本、删除操作、序列号计数器。
真实数据行在 Parquet 文件中，元数据只记录身份、版本与序列号。

事务约定
--------
* 所有变更方法在一把进程内锁 + sqlite 单连接串行执行（本地合成服务）；
* 序列号分配与业务记录写入在同一事务中提交，序列号不跳号、不回滚复用；
* 冲突先查后改，靠锁保证可串行化，冲突时抛 StateConflictError。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..errors import NotFoundError, StateConflictError

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tables (
    table_id     TEXT PRIMARY KEY,
    columns_json TEXT NOT NULL,
    key_cols_json TEXT NOT NULL,
    next_seq     INTEGER NOT NULL DEFAULT 1,
    created_ts   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    file_id       TEXT NOT NULL,
    table_id      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    is_live       INTEGER NOT NULL,
    row_count     INTEGER NOT NULL,
    fingerprint   TEXT NOT NULL,
    created_seq   INTEGER NOT NULL,
    superseded_by TEXT,
    created_ts    TEXT NOT NULL,
    PRIMARY KEY (file_id, version)
);
CREATE TABLE IF NOT EXISTS deletes (
    delete_id     TEXT PRIMARY KEY,
    table_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    file_id       TEXT,
    row_number    INTEGER,
    bound_version INTEGER,
    key_cols_json TEXT,
    key_vals_json TEXT,
    spec_json     TEXT NOT NULL,
    created_ts    TEXT NOT NULL
);
"""


class MetadataStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """串行化事务上下文；异常回滚。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---------------- 序列号 ----------------
    def allocate_seq(self, conn: sqlite3.Connection, table_id: str, n: int = 1) -> int:
        """在事务中分配 n 个连续序列号，返回第一个。"""
        row = conn.execute("SELECT next_seq FROM tables WHERE table_id=?", (table_id,)).fetchone()
        if row is None:
            raise NotFoundError("表不存在", table_id=table_id)
        first = row["next_seq"]
        conn.execute("UPDATE tables SET next_seq=? WHERE table_id=?", (first + n, table_id))
        return first

    def get_seq_horizon(self, table_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT next_seq FROM tables WHERE table_id=?", (table_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("表不存在", table_id=table_id)
        return row["next_seq"] - 1

    # ---------------- 表 ----------------
    def create_table(self, table_id: str, columns: dict[str, str], key_cols: list[str], ts: str) -> None:
        with self.txn() as conn:
            exists = conn.execute("SELECT 1 FROM tables WHERE table_id=?", (table_id,)).fetchone()
            if exists:
                raise StateConflictError("表已存在", table_id=table_id)
            conn.execute(
                "INSERT INTO tables(table_id, columns_json, key_cols_json, created_ts) "
                "VALUES (?,?,?,?)",
                (table_id, json.dumps(columns), json.dumps(key_cols), ts),
            )

    def get_table(self, table_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tables WHERE table_id=?", (table_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("表不存在", table_id=table_id)
        return self._table_row(row)

    def list_tables(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tables ORDER BY table_id").fetchall()
        return [self._table_row(r) for r in rows]

    @staticmethod
    def _table_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "table_id": row["table_id"],
            "columns": json.loads(row["columns_json"]),
            "key_columns": json.loads(row["key_cols_json"]),
            "next_seq": row["next_seq"],
            "created_ts": row["created_ts"],
        }

    # ---------------- 文件 ----------------
    def insert_file(
        self, conn: sqlite3.Connection, *, table_id: str, file_id: str, version: int,
        row_count: int, fingerprint: str, created_seq: int, ts: str,
    ) -> None:
        conn.execute(
            "INSERT INTO files(file_id, table_id, version, is_live, row_count, fingerprint, "
            "created_seq, created_ts) VALUES (?,?,?,1,?,?,?,?)",
            (file_id, table_id, version, row_count, fingerprint, created_seq, ts),
        )

    def supersede_files(
        self, conn: sqlite3.Connection, table_id: str, old_keys: list[tuple[str, int]],
        new_file_id: str | None,
    ) -> None:
        for file_id, version in old_keys:
            conn.execute(
                "UPDATE files SET is_live=0, superseded_by=? "
                "WHERE table_id=? AND file_id=? AND version=?",
                (new_file_id, table_id, file_id, version),
            )

    def list_files(self, table_id: str, live_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM files WHERE table_id=?"
        if live_only:
            sql += " AND is_live=1"
        sql += " ORDER BY file_id, version"
        with self._lock:
            rows = self._conn.execute(sql, (table_id,)).fetchall()
        return [self._file_row(r) for r in rows]

    def get_file(self, table_id: str, file_id: str, version: int | None = None) -> dict[str, Any]:
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    "SELECT * FROM files WHERE table_id=? AND file_id=? ORDER BY version DESC LIMIT 1",
                    (table_id, file_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM files WHERE table_id=? AND file_id=? AND version=?",
                    (table_id, file_id, version),
                ).fetchone()
        if row is None:
            raise NotFoundError("文件不存在", file_id=file_id, version=version)
        return self._file_row(row)

    def file_exists_any_version(self, table_id: str, file_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM files WHERE table_id=? AND file_id=? LIMIT 1",
                (table_id, file_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _file_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "file_id": row["file_id"], "table_id": row["table_id"], "version": row["version"],
            "is_live": bool(row["is_live"]), "row_count": row["row_count"],
            "fingerprint": row["fingerprint"], "created_seq": row["created_seq"],
            "superseded_by": row["superseded_by"], "created_ts": row["created_ts"],
        }

    # ---------------- 删除操作 ----------------
    def insert_delete(
        self, conn: sqlite3.Connection, *, table_id: str, delete_id: str, kind: str, seq: int,
        file_id: str | None, row_number: int | None, bound_version: int | None,
        key_cols: list[str] | None, key_vals: list[Any] | None, spec: dict[str, Any], ts: str,
    ) -> None:
        conn.execute(
            "INSERT INTO deletes(delete_id, table_id, kind, seq, file_id, row_number, "
            "bound_version, key_cols_json, key_vals_json, spec_json, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (delete_id, table_id, kind, seq, file_id, row_number, bound_version,
             json.dumps(key_cols) if key_cols is not None else None,
             json.dumps(key_vals) if key_vals is not None else None,
             json.dumps(spec, ensure_ascii=False), ts),
        )

    def get_delete(self, table_id: str, delete_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deletes WHERE table_id=? AND delete_id=?", (table_id, delete_id),
            ).fetchone()
        return self._delete_row(row) if row else None

    def list_deletes(self, table_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM deletes WHERE table_id=? ORDER BY seq", (table_id,),
            ).fetchall()
        return [self._delete_row(r) for r in rows]

    @staticmethod
    def _delete_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "delete_id": row["delete_id"], "table_id": row["table_id"], "kind": row["kind"],
            "seq": row["seq"], "file_id": row["file_id"], "row_number": row["row_number"],
            "bound_version": row["bound_version"],
            "key_columns": json.loads(row["key_cols_json"]) if row["key_cols_json"] else None,
            "key_values": json.loads(row["key_vals_json"]) if row["key_vals_json"] else None,
            "spec": json.loads(row["spec_json"]),
            "created_ts": row["created_ts"],
        }
