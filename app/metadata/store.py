"""SQLite 元数据仓库：表、快照、清单、删除文件、事件、运行日志。

事务边界：提交一次快照在单个 BEGIN IMMEDIATE 事务内完成所有元数据写入；
物理 Parquet 文件先落盘，元数据提交后才对外可见（事务失败会清理新物理文件）。
序列号 seq 与 snapshot_id 在事务内分配，天然串行化并发提交。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import Config
from app.errors import StateConflict

_SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, config: Config):
        self.config = config
        config.warehouse_dir.mkdir(parents=True, exist_ok=True)
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ---- 连接与初始化 -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        sql = _SCHEMA_SQL.read_text(encoding="utf-8")
        conn = self._connect()
        try:
            # executescript 自动忽略 -- 行注释，并整体提交
            conn.executescript(sql)
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """立即取写锁的元数据事务；SQLITE_BUSY 归类为 STATE_CONFLICT。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            conn.execute("ROLLBACK")
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StateConflict("METADATA_LOCKED", "metadata store is locked by another commit")
            raise
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # ---- 表 -----------------------------------------------------------
    def create_table(
        self,
        conn: sqlite3.Connection,
        *,
        table_id: str,
        name: str,
        columns: list[dict[str, Any]],
        primary_key: list[str],
        config: dict[str, int],
    ) -> None:
        conn.execute(
            "INSERT INTO tables(table_id, name, schema_json, primary_key, config_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (table_id, name, json.dumps(columns), json.dumps(primary_key), json.dumps(config), _utcnow()),
        )

    def get_table(self, table_id: str) -> sqlite3.Row | None:
        with self.read_conn() as conn:
            return conn.execute("SELECT * FROM tables WHERE table_id=?", (table_id,)).fetchone()

    # ---- 快照 ---------------------------------------------------------
    def get_snapshot(self, conn: sqlite3.Connection, snapshot_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()

    def current_snapshot(self, conn: sqlite3.Connection, table_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM snapshots WHERE table_id=? ORDER BY seq DESC LIMIT 1", (table_id,)
        ).fetchone()

    def insert_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        snapshot_id: str,
        table_id: str,
        seq: int,
        parent_id: str | None,
        summary: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO snapshots(snapshot_id, table_id, seq, parent_id, created_at, summary_json)"
            " VALUES (?,?,?,?,?,?)",
            (snapshot_id, table_id, seq, parent_id, _utcnow(), json.dumps(summary)),
        )

    def list_snapshots(self, table_id: str) -> list[sqlite3.Row]:
        with self.read_conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM snapshots WHERE table_id=? ORDER BY seq", (table_id,)
                ).fetchall()
            )

    # ---- 数据文件与清单 -----------------------------------------------
    def insert_data_file(
        self,
        conn: sqlite3.Connection,
        *,
        file_id: str,
        table_id: str,
        path: str,
        content_hash: str,
        row_count: int,
        added_seq: int,
    ) -> None:
        conn.execute(
            "INSERT INTO data_files(file_id, table_id, path, content_hash, row_count, added_seq)"
            " VALUES (?,?,?,?,?,?)",
            (file_id, table_id, path, content_hash, row_count, added_seq),
        )

    def get_data_file(self, conn: sqlite3.Connection, file_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM data_files WHERE file_id=?", (file_id,)).fetchone()

    def add_manifest(
        self,
        conn: sqlite3.Connection,
        *,
        table_id: str,
        file_id: str,
        seq: int,
        snapshot_id: str,
        change: str,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT INTO manifest_entries(table_id, file_id, seq, snapshot_id, change, reason)"
            " VALUES (?,?,?,?,?,?)",
            (table_id, file_id, seq, snapshot_id, change, reason),
        )

    def live_files_at(self, conn: sqlite3.Connection, table_id: str, seq: int) -> list[sqlite3.Row]:
        """版本 seq 下存活文件：最新清单事件为 ADD。"""
        rows = conn.execute(
            """
            SELECT df.*, me_last.change AS last_change
            FROM data_files df
            JOIN (
                SELECT file_id, change,
                       ROW_NUMBER() OVER (PARTITION BY file_id ORDER BY seq DESC,
                           CASE change WHEN 'DROP' THEN 0 ELSE 1 END) rn
                FROM manifest_entries
                WHERE table_id=? AND seq<=?
            ) me_last ON me_last.file_id=df.file_id AND me_last.rn=1
            WHERE df.table_id=? AND me_last.change='ADD'
            ORDER BY df.file_id
            """,
            (table_id, seq, table_id),
        ).fetchall()
        return list(rows)

    def all_manifest_entries(self, conn: sqlite3.Connection, table_id: str) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT * FROM manifest_entries WHERE table_id=? ORDER BY seq, file_id", (table_id,)
            ).fetchall()
        )

    # ---- 删除文件 -----------------------------------------------------
    def insert_delete_file(
        self,
        conn: sqlite3.Connection,
        *,
        delete_file_id: str,
        table_id: str,
        path: str,
        content_hash: str,
        kind: str,
        target_file_id: str | None,
        key_columns: list[str] | None,
        row_count: int,
        seq: int,
        snapshot_id: str,
    ) -> None:
        conn.execute(
            "INSERT INTO delete_files(delete_file_id, table_id, path, content_hash, kind,"
            " target_file_id, key_columns, row_count, seq, snapshot_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                delete_file_id,
                table_id,
                path,
                content_hash,
                kind,
                target_file_id,
                json.dumps(key_columns) if key_columns is not None else None,
                row_count,
                seq,
                snapshot_id,
            ),
        )

    def delete_files_for_scan(
        self, conn: sqlite3.Connection, table_id: str, seq: int
    ) -> list[sqlite3.Row]:
        """读 seq 版本时可见的删除文件（seq<=版本 seq），按 seq、id 稳定排序。"""
        return list(
            conn.execute(
                "SELECT * FROM delete_files WHERE table_id=? AND seq<=? ORDER BY seq, delete_file_id",
                (table_id, seq),
            ).fetchall()
        )

    # ---- 事件与运行日志 -----------------------------------------------
    def insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        table_id: str,
        event_type: str,
        payload: dict[str, Any],
        snapshot_id: str | None = None,
        seq: int | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO event_log(run_id, ts, table_id, snapshot_id, seq, event_type, payload_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, _utcnow(), table_id, snapshot_id, seq, event_type, json.dumps(payload)),
        )

    def list_events(self, table_id: str, seq: int | None = None) -> list[sqlite3.Row]:
        with self.read_conn() as conn:
            if seq is None:
                return list(
                    conn.execute(
                        "SELECT * FROM event_log WHERE table_id=? ORDER BY id", (table_id,)
                    ).fetchall()
                )
            return list(
                conn.execute(
                    "SELECT * FROM event_log WHERE table_id=? AND seq<=? ORDER BY id", (table_id, seq)
                ).fetchall()
            )

    def insert_request_log(
        self,
        *,
        run_id: str,
        kind: str,
        table_id: str | None,
        status: str,
        request: dict[str, Any],
        phases: list[dict[str, Any]],
        error: dict[str, Any] | None,
        duration_ms: float,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO request_log(run_id, ts, kind, table_id, status, request_json,"
                " phases_json, error_json, duration_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    _utcnow(),
                    kind,
                    table_id,
                    status,
                    json.dumps(request, default=str),
                    json.dumps(phases, default=str),
                    json.dumps(error) if error else None,
                    duration_ms,
                ),
            )

    def get_request_log(self, run_id: str) -> sqlite3.Row | None:
        with self.read_conn() as conn:
            return conn.execute("SELECT * FROM request_log WHERE run_id=?", (run_id,)).fetchone()

    def list_request_logs(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.read_conn() as conn:
            return list(
                conn.execute("SELECT * FROM request_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            )
