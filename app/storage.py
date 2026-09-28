"""版本存储层（SQLite）。

数据模型
========

- ``meta``           单行式 KV：规范化版本、当前 HEAD 版本等。
- ``versions``       版本链：每次变更批次/快照/恢复产生一个版本，``parent_id`` 串链。
- ``entries``        当前词条表（HEAD 视图）：id 主键、原文、规范化键、词频、
                     写入时的规范化版本、更新版本号。
- ``entry_events``   仅追加事件日志（upsert/delete 的前后值），用于审计与诊断。
- ``snapshots``      物化快照标记：某版本时刻的词条全量被复制到
                     ``snapshot_entries``，可直接在其上构建只读 Trie 查询。
- ``snapshot_entries``  快照词条数据。

并发：每个连接开启 WAL 与外键；写操作在单个事务内提交。应用层
（:mod:`app.engine`）另用 ``RLock`` 串行化变更与重建。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .normalize import NORMALIZER_VERSION, normalize_text

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id    INTEGER,
    kind         TEXT NOT NULL CHECK (kind IN ('baseline','commit','snapshot','restore')),
    normalizer_version TEXT NOT NULL,
    client_batch_id TEXT,
    entry_count  INTEGER NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES versions(version_id)
);

CREATE TABLE IF NOT EXISTS entries (
    id            TEXT PRIMARY KEY,
    display       TEXT NOT NULL,
    term_norm     TEXT NOT NULL,
    score         REAL NOT NULL,
    norm_version  TEXT NOT NULL,
    version_id    INTEGER NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES versions(version_id)
);
CREATE INDEX IF NOT EXISTS idx_entries_term_norm ON entries(term_norm);

CREATE TABLE IF NOT EXISTS entry_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL,
    entry_id     TEXT NOT NULL,
    op           TEXT NOT NULL CHECK (op IN ('upsert','delete')),
    before_json   TEXT,
    after_json    TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (version_id) REFERENCES versions(version_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    version_id    INTEGER PRIMARY KEY,
    entry_count   INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (version_id) REFERENCES versions(version_id)
);

CREATE TABLE IF NOT EXISTS snapshot_entries (
    snapshot_version_id INTEGER NOT NULL,
    id            TEXT NOT NULL,
    display       TEXT NOT NULL,
    term_norm     TEXT NOT NULL,
    score         REAL NOT NULL,
    PRIMARY KEY (snapshot_version_id, id),
    FOREIGN KEY (snapshot_version_id) REFERENCES snapshots(version_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_entries_term
    ON snapshot_entries(snapshot_version_id, term_norm);
"""


def utc_now_iso() -> str:
    """UTC ISO-8601 时间戳（秒级，确定性足够且无时区歧义）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建目录与）数据库连接并应用 PRAGMA。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class Storage:
    """封装所有 SQLite 访问；线程安全依赖调用方（engine 的锁）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._write_lock = threading.Lock()
        self._initialize()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务上下文：正常结束提交，异常回滚（不吞异常）。"""
        with self._write_lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def _initialize(self) -> None:
        with self.transaction() as conn:
            conn.executescript(_DDL)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {row['value']!r} 与代码 {SCHEMA_VERSION} 不兼容"
                )
            head = conn.execute("SELECT value FROM meta WHERE key='head_version'").fetchone()
            if head is None:
                ts = utc_now_iso()
                cur = conn.execute(
                    "INSERT INTO versions(version_id, parent_id, kind, normalizer_version,"
                    " client_batch_id, entry_count, note, created_at)"
                    " VALUES (1, NULL, 'baseline', ?, NULL, 0, 'initial empty baseline', ?)",
                    (NORMALIZER_VERSION, ts),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('head_version', '1')"
                )
            norm = conn.execute(
                "SELECT value FROM meta WHERE key='normalizer_version'"
            ).fetchone()
            if norm is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('normalizer_version', ?)",
                    (NORMALIZER_VERSION,),
                )

    # ------------------------------------------------------------------ 元信息

    @property
    def normalizer_version(self) -> str:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='normalizer_version'"
        ).fetchone()
        return row["value"]

    @property
    def head_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='head_version'"
        ).fetchone()
        return int(row["value"])

    def list_versions(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM versions ORDER BY version_id DESC LIMIT ?", (limit,)
            )
        )

    def get_version(self, version_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM versions WHERE version_id=?", (version_id,)
        ).fetchone()

    def list_snapshots(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT v.version_id, v.kind, v.parent_id, v.created_at, v.note,"
                " s.entry_count, s.created_at AS snapshot_created_at"
                " FROM snapshots s JOIN versions v ON v.version_id=s.version_id"
                " ORDER BY s.version_id DESC"
            )
        )

    # ------------------------------------------------------------------ 词条读

    def iter_entries(self) -> Iterator[sqlite3.Row]:
        """全量扫描当前 HEAD 词条（仅重建索引/参考比对用）。"""
        yield from self.conn.execute(
            "SELECT id, display, term_norm, score, norm_version, version_id, updated_at"
            " FROM entries ORDER BY id"
        )

    def get_entry(self, entry_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, display, term_norm, score, norm_version, version_id, updated_at"
            " FROM entries WHERE id=?",
            (entry_id,),
        ).fetchone()

    def count_entries(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"])

    # ------------------------------------------------------------------ 变更

    def new_version(
        self,
        kind: str,
        client_batch_id: Optional[str],
        note: str,
        entry_count: int,
        conn: Optional[sqlite3.Connection] = None,
    ) -> int:
        c = conn or self.conn
        parent = self.head_version
        ts = utc_now_iso()
        cur = c.execute(
            "INSERT INTO versions(parent_id, kind, normalizer_version, client_batch_id,"
            " entry_count, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (parent, kind, self.normalizer_version, client_batch_id, entry_count, note, ts),
        )
        return int(cur.lastrowid)

    def apply_upsert(
        self,
        conn: sqlite3.Connection,
        version_id: int,
        entry_id: str,
        display: str,
        term_norm: str,
        score: float,
    ) -> str:
        """在给定事务内 upsert 一个词条并追加事件，返回 'inserted' | 'updated'。"""
        ts = utc_now_iso()
        before = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if before is None:
            conn.execute(
                "INSERT INTO entries(id, display, term_norm, score, norm_version,"
                " version_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry_id, display, term_norm, score, NORMALIZER_VERSION, version_id, ts),
            )
            op = "inserted"
        else:
            conn.execute(
                "UPDATE entries SET display=?, term_norm=?, score=?,"
                " norm_version=?, version_id=?, updated_at=? WHERE id=?",
                (display, term_norm, score, NORMALIZER_VERSION, version_id, ts, entry_id),
            )
            op = "updated"
        conn.execute(
            "INSERT INTO entry_events(version_id, entry_id, op, before_json, after_json,"
            " created_at) VALUES (?, ?, 'upsert', ?, ?, ?)",
            (
                version_id,
                entry_id,
                None if before is None else json.dumps(dict(before), ensure_ascii=False),
                json.dumps(
                    {"id": entry_id, "display": display, "term_norm": term_norm,
                     "score": score, "norm_version": NORMALIZER_VERSION},
                    ensure_ascii=False,
                ),
                ts,
            ),
        )
        return op

    def apply_delete(
        self, conn: sqlite3.Connection, version_id: int, entry_id: str
    ) -> bool:
        ts = utc_now_iso()
        before = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if before is None:
            return False
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        conn.execute(
            "INSERT INTO entry_events(version_id, entry_id, op, before_json, after_json,"
            " created_at) VALUES (?, ?, 'delete', ?, NULL, ?)",
            (
                version_id,
                entry_id,
                json.dumps(dict(before), ensure_ascii=False),
                ts,
            ),
        )
        return True

    def set_head(self, conn: sqlite3.Connection, version_id: int) -> None:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='head_version'", (str(version_id),)
        )

    def set_version_count(self, conn: sqlite3.Connection, version_id: int, count: int) -> None:
        conn.execute(
            "UPDATE versions SET entry_count=? WHERE version_id=?", (count, version_id)
        )

    # ------------------------------------------------------------------ 快照

    def create_snapshot(self, note: str = "") -> int:
        """把当前 HEAD 词条全量物化为一个 snapshot 版本。"""
        with self.transaction() as conn:
            count = self.count_entries()
            version_id = self.new_version("snapshot", None, note or "snapshot", count, conn)
            ts = utc_now_iso()
            conn.execute(
                "INSERT INTO snapshots(version_id, entry_count, created_at, note)"
                " VALUES (?, ?, ?, ?)",
                (version_id, count, ts, note or "snapshot"),
            )
            conn.execute(
                "INSERT INTO snapshot_entries(snapshot_version_id, id, display,"
                " term_norm, score) SELECT ?, id, display, term_norm, score FROM entries",
                (version_id,),
            )
            self.set_head(conn, version_id)
            return version_id

    def snapshot_exists(self, version_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM snapshots WHERE version_id=?", (version_id,)
        ).fetchone()
        return row is not None

    def iter_snapshot_entries(self, version_id: int) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute(
            "SELECT id, display, term_norm, score FROM snapshot_entries"
            " WHERE snapshot_version_id=? ORDER BY id",
            (version_id,),
        )

    def restore_snapshot(self, version_id: int, note: str = "") -> int:
        """从快照恢复：新建 restore 版本，把当前词条表整体替换为快照内容。"""
        if not self.snapshot_exists(version_id):
            raise KeyError(f"快照版本 {version_id} 不存在")
        with self.transaction() as conn:
            snap = conn.execute(
                "SELECT entry_count FROM snapshots WHERE version_id=?", (version_id,)
            ).fetchone()
            new_id = self.new_version(
                "restore", None, note or f"restore from v{version_id}",
                int(snap["entry_count"]), conn,
            )
            # 记录被丢弃的当前词条到事件日志（审计），再整体替换
            for row in conn.execute("SELECT * FROM entries").fetchall():
                conn.execute(
                    "INSERT INTO entry_events(version_id, entry_id, op, before_json,"
                    " after_json, created_at) VALUES (?, ?, 'delete', ?, NULL, ?)",
                    (new_id, row["id"], json.dumps(dict(row), ensure_ascii=False),
                     utc_now_iso()),
                )
            conn.execute("DELETE FROM entries")
            rows = conn.execute(
                "SELECT id, display, term_norm, score FROM snapshot_entries"
                " WHERE snapshot_version_id=?",
                (version_id,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO entries(id, display, term_norm, score, norm_version,"
                    " version_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (r["id"], r["display"], r["term_norm"], r["score"],
                     NORMALIZER_VERSION, new_id, utc_now_iso()),
                )
                conn.execute(
                    "INSERT INTO entry_events(version_id, entry_id, op, before_json,"
                    " after_json, created_at) VALUES (?, ?, 'upsert', NULL, ?, ?)",
                    (new_id, r["id"],
                     json.dumps(
                         {"id": r["id"], "display": r["display"],
                          "term_norm": r["term_norm"], "score": r["score"],
                          "norm_version": NORMALIZER_VERSION},
                         ensure_ascii=False),
                     utc_now_iso()),
                )
            self.set_head(conn, new_id)
            return new_id


def new_client_batch_id() -> str:
    """生成客户端批次 ID（客户端未提供时使用）。"""
    return uuid.uuid4().hex
