"""版本存储层：SQLite 持久化 + 索引数组紧凑编码 + 操作日志。

每个文档版本不可变（append-only），版本行绑定：
- 原文 SHA-256 摘要（加载时复算校验，防止索引与原文错配）；
- 构建时的 GCB 表版本与 unicodedata 版本；
- 完整双向索引数组（LEB128 无符号变长整数编码，可独立解码审计）。

operation_log 记录每次操作的 run_id、关键中间状态与错误类别，
诊断接口可按 run_id 回放问题。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .indexing import TextIndex

# ── LEB128 数组编码（小端无符号变长整数；delta 编码进一步压缩单调数组）────────

def _enc_unsigned(value: int) -> bytes:
    if value < 0:
        raise ValueError("仅支持非负整数")
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _dec_unsigned(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def encode_array(values: list[int] | tuple[int, ...]) -> bytes:
    """非递减数组按首值 + delta 的 LEB128 编码。"""
    out = bytearray()
    prev = 0
    for v in values:
        out += _enc_unsigned(v - prev)
        prev = v
    return bytes(out)


def decode_array(buf: bytes) -> list[int]:
    out: list[int] = []
    pos = 0
    total = 0
    while pos < len(buf):
        d, pos = _dec_unsigned(buf, pos)
        total += d
        out.append(total)
    return out


# ── 行模型 ───────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class VersionStore:
    """线程安全包装：每次操作使用独立连接（check_same_thread=False + 锁）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

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

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    current_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS versions (
                    doc_id TEXT NOT NULL REFERENCES documents(doc_id),
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    parent_version INTEGER,
                    build_mode TEXT NOT NULL,            -- full | incremental_verified
                    content_sha256 TEXT NOT NULL,
                    gcb_table_version TEXT NOT NULL,
                    unidata_version TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    cp_count INTEGER NOT NULL,
                    cluster_count INTEGER NOT NULL,
                    cp_to_byte BLOB NOT NULL,
                    cp_to_cluster BLOB NOT NULL,
                    cluster_to_cp BLOB NOT NULL,
                    edit_info TEXT,                       -- 产生该版本的编辑（回放用）
                    PRIMARY KEY (doc_id, version)
                );
                CREATE TABLE IF NOT EXISTS contents (
                    doc_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    raw BLOB NOT NULL,
                    PRIMARY KEY (doc_id, version)
                );
                CREATE TABLE IF NOT EXISTS operation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    op TEXT NOT NULL,
                    doc_id TEXT,
                    version INTEGER,
                    success INTEGER NOT NULL,
                    error_category TEXT,
                    error_code TEXT,
                    detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_oplog_run ON operation_log(run_id);
                CREATE INDEX IF NOT EXISTS ix_oplog_doc ON operation_log(doc_id);
                """
            )
            self._conn.commit()

    # ── 文档/版本 ────────────────────────────────────────────────────────────
    def create_document(self, doc_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents(doc_id, created_at, updated_at, current_version) "
                "VALUES (?, ?, ?, 0)",
                (doc_id, _now(), _now()),
            )
            self._conn.commit()

    def document_exists(self, doc_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)
            ).fetchone()
            return row is not None

    def get_document(self, doc_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM documents WHERE doc_id=?", (doc_id,)
            ).fetchone()

    def save_version(
        self,
        *,
        doc_id: str,
        version: int,
        index: TextIndex,
        content_sha256: str,
        gcb_table_version: str,
        unidata_version: str,
        build_mode: str,
        parent_version: int | None,
        edit_info: dict[str, Any] | None,
    ) -> None:
        with self._lock:
            self._insert_version(
                doc_id, version, index, content_sha256,
                gcb_table_version, unidata_version,
                build_mode, parent_version, edit_info,
            )
            self._conn.commit()

    def create_document_with_version(
        self,
        *,
        doc_id: str,
        index: TextIndex,
        raw: bytes,
        content_sha256: str,
        gcb_table_version: str,
        unidata_version: str,
    ) -> None:
        """原子创建文档及其第 0 版（任一步失败全部回滚）。"""
        with self._lock:
            try:
                now = _now()
                self._conn.execute(
                    "INSERT INTO documents(doc_id, created_at, updated_at, current_version)"
                    " VALUES (?, ?, ?, 0)",
                    (doc_id, now, now),
                )
                self._insert_version(
                    doc_id, 0, index, content_sha256,
                    gcb_table_version, unidata_version,
                    "full", None, {"op": "create"},
                )
                self._conn.execute(
                    "INSERT INTO contents(doc_id, version, raw) VALUES (?,?,?)",
                    (doc_id, 0, raw),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def save_version_with_content(
        self,
        *,
        doc_id: str,
        version: int,
        index: TextIndex,
        raw: bytes,
        content_sha256: str,
        gcb_table_version: str,
        unidata_version: str,
        build_mode: str,
        parent_version: int | None,
        edit_info: dict[str, Any] | None,
    ) -> None:
        """原子写入版本索引行 + 原文内容 + 更新当前版本指针。"""
        with self._lock:
            try:
                self._insert_version(
                    doc_id, version, index, content_sha256,
                    gcb_table_version, unidata_version,
                    build_mode, parent_version, edit_info,
                )
                self._conn.execute(
                    "INSERT INTO contents(doc_id, version, raw) VALUES (?,?,?)",
                    (doc_id, version, raw),
                )
                self._conn.execute(
                    "UPDATE documents SET current_version=?, updated_at=? WHERE doc_id=?",
                    (version, _now(), doc_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _insert_version(
        self, doc_id, version, index, content_sha256,
        gcb_table_version, unidata_version,
        build_mode, parent_version, edit_info,
    ) -> None:
        self._conn.execute(
            "INSERT INTO versions(doc_id, version, created_at, parent_version, build_mode,"
            " content_sha256, gcb_table_version, unidata_version, byte_count, cp_count,"
            " cluster_count, cp_to_byte, cp_to_cluster, cluster_to_cp, edit_info)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id, version, _now(), parent_version, build_mode,
                content_sha256, gcb_table_version, unidata_version,
                index.byte_count, index.cp_count, index.cluster_count,
                encode_array(index.cp_to_byte),
                encode_array(index.cp_to_cluster),
                encode_array(index.cluster_to_cp),
                json.dumps(edit_info, ensure_ascii=False) if edit_info else None,
            ),
        )

    def get_version_row(self, doc_id: str, version: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM versions WHERE doc_id=? AND version=?",
                (doc_id, version),
            ).fetchone()

    def list_versions(self, doc_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT version, created_at, parent_version, build_mode,"
                    " content_sha256, byte_count, cp_count, cluster_count,"
                    " gcb_table_version, unidata_version"
                    " FROM versions WHERE doc_id=? ORDER BY version",
                    (doc_id,),
                )
            )

    def count_documents(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def save_content(self, doc_id: str, version: int, raw: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO contents(doc_id, version, raw) VALUES (?,?,?)",
                (doc_id, version, raw),
            )
            self._conn.commit()

    def get_content(self, doc_id: str, version: int) -> bytes | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT raw FROM contents WHERE doc_id=? AND version=?",
                (doc_id, version),
            ).fetchone()
            return None if row is None else bytes(row["raw"])

    # ── 操作日志（回放诊断）──────────────────────────────────────────────────
    def log_operation(
        self,
        *,
        run_id: str,
        op: str,
        doc_id: str | None,
        version: int | None,
        success: bool,
        detail: dict[str, Any],
        error_category: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO operation_log(run_id, ts, op, doc_id, version, success,"
                " error_category, error_code, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id, _now(), op, doc_id, version, 1 if success else 0,
                    error_category, error_code,
                    json.dumps(detail, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()

    def find_operations(self, run_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM operation_log WHERE run_id=? ORDER BY id", (run_id,)
                )
            )

    def recent_operations(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM operation_log ORDER BY id DESC LIMIT ?", (limit,)
                )
            )
