"""SQLite 节点存储与版本/索引/流水。

单连接 + 进程内互斥锁（check_same_thread=False，FastAPI 线程池下安全）。
写操作通过 ``transaction()`` 上下文一次性提交，崩溃不留半截版本。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.coding.nodes import BranchNode, LeafNode
from app.core.store import NodeStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    digest BLOB PRIMARY KEY,
    kind   TEXT NOT NULL,           -- leaf / branch
    depth  INTEGER,                 -- branch: 层号；leaf: NULL
    nkey   BLOB,                    -- leaf: 键
    nvalue BLOB,                    -- leaf: 值（空字节串与 NULL 严格区分：kind+键已保证）
    left   BLOB,                    -- branch: 32B 左子摘要
    "right" BLOB                    -- right 为 SQL 保留字，列名加引号
);
CREATE TABLE IF NOT EXISTS versions (
    version     INTEGER PRIMARY KEY,
    root        BLOB NOT NULL,
    parent_root BLOB,
    batch_id    TEXT NOT NULL,
    signature   BLOB NOT NULL,
    changed     INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS key_index (
    nkey        BLOB PRIMARY KEY,
    nvalue      BLOB NOT NULL,
    version     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     INTEGER NOT NULL,
    batch_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,       -- 批内规范化序号
    nkey        BLOB NOT NULL,
    nvalue      BLOB                     -- NULL=删除；b""=空值（允许的真实值）
);
CREATE TABLE IF NOT EXISTS idempotency (
    idem_key    TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_version ON journal(version);
CREATE INDEX IF NOT EXISTS idx_key_index_version ON key_index(version);
"""


class SqliteStore(NodeStore):
    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------- NodeStore 协议 ----------

    def get(self, digest: bytes) -> LeafNode | BranchNode | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, depth, nkey, nvalue, left, \"right\" FROM nodes WHERE digest=?",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        if row["kind"] == "leaf":
            return LeafNode(key=row["nkey"], value=row["nvalue"])
        return BranchNode(depth=row["depth"], left=row["left"], right=row["right"])

    def put_leaf(self, node: LeafNode) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO nodes(digest, kind, depth, nkey, nvalue, left, \"right\")"
                " VALUES(?,'leaf',NULL,?,?,NULL,NULL)",
                (node.digest, node.key, node.value),
            )

    def put_branch(self, node: BranchNode) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO nodes(digest, kind, depth, nkey, nvalue, left, \"right\")"
                " VALUES(?,'branch',?,NULL,NULL,?,?)",
                (node.digest_at(), node.depth, node.left, node.right),
            )

    # ---------- 版本链 ----------

    def current_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(version) AS v FROM versions").fetchone()
        return int(row["v"] or 0)

    def root_for_version(self, version: int) -> bytes | None:
        with self._lock:
            if version == 0:
                row = self._conn.execute(
                    "SELECT root FROM versions WHERE version=0"
                ).fetchone()
                if row is None:
                    return None
                return row["root"]
            row = self._conn.execute(
                "SELECT root FROM versions WHERE version=?", (version,)
            ).fetchone()
        return row["root"] if row else None

    def checkpoint(self, version: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT version, root, parent_root, batch_id, signature, changed, created_at"
                " FROM versions WHERE version=?",
                (version,),
            ).fetchone()

    def latest_checkpoint(self) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, root, parent_root, batch_id, signature, changed, created_at"
                " FROM versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("版本表为空（v0 检查点未初始化）")
        return row

    def all_checkpoints(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT version, root, parent_root, batch_id, signature, changed, created_at"
                " FROM versions ORDER BY version"
            ))

    def add_version(
        self,
        version: int,
        root: bytes,
        parent_root: bytes | None,
        batch_id: str,
        signature: bytes,
        changed: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO versions(version, root, parent_root, batch_id, signature,"
                " changed, created_at) VALUES(?,?,?,?,?,?,?)",
                (version, root, parent_root, batch_id, signature, changed, time.time()),
            )

    def ensure_genesis(self, root0: bytes, batch_id: str, signature: bytes) -> None:
        """幂等写入 v0（空根检查点）。"""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM versions WHERE version=0"
            ).fetchone()
            if exists is None:
                self._conn.execute(
                    "INSERT INTO versions(version, root, parent_root, batch_id, signature,"
                    " changed, created_at) VALUES(0,?,NULL,?,?,0,?)",
                    (root0, batch_id, signature, time.time()),
                )
                self._conn.commit()

    # ---------- 键索引 ----------

    def upsert_index(self, key: bytes, value: bytes, version: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO key_index(nkey, nvalue, version) VALUES(?,?,?)"
                " ON CONFLICT(nkey) DO UPDATE SET nvalue=excluded.nvalue, version=excluded.version",
                (key, value, version),
            )

    def delete_index(self, key: bytes) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM key_index WHERE nkey=?", (key,))

    def live_keys(self) -> list[tuple[bytes, bytes]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT nkey, nvalue FROM key_index ORDER BY nkey"
            ).fetchall()
        return [(r["nkey"], r["nvalue"]) for r in rows]

    def indexed_value(self, key: bytes) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT nvalue FROM key_index WHERE nkey=?", (key,)
            ).fetchone()
        return None if row is None else row["nvalue"]

    # ---------- 流水 ----------

    def append_journal(self, version: int, batch_id: str, seq: int,
                       key: bytes, value: bytes | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO journal(version, batch_id, seq, nkey, nvalue)"
                " VALUES(?,?,?,?,?)",
                (version, batch_id, seq, key, value),
            )

    def journal_for_version(self, version: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT seq, nkey, nvalue FROM journal WHERE version=? ORDER BY seq",
                (version,),
            ))

    def all_journal(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT version, batch_id, seq, nkey, nvalue FROM journal"
                " ORDER BY version, seq"
            ))

    # ---------- 幂等 ----------

    def idempotency_lookup(self, idem_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT idem_key, batch_id, version, payload_hash FROM idempotency"
                " WHERE idem_key=?",
                (idem_key,),
            ).fetchone()

    def idempotency_store(self, idem_key: str, batch_id: str, version: int,
                          payload_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO idempotency(idem_key, batch_id, version, payload_hash, created_at)"
                " VALUES(?,?,?,?,?)",
                (idem_key, batch_id, version, payload_hash, time.time()),
            )

    # ---------- 诊断 ----------

    def node_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"])
