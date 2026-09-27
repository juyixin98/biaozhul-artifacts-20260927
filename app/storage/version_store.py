"""SQLite 版本存储。

设计要点（对应需求约束）：

* **显式版本化文档全集**：每个版本在 ``documents(version_id, doc_id, visible)``
  中逐行记录该版本全集的成员关系。NOT 永远相对于某一具体版本的全集求补，
  全集是有限集合，绝不会“补成无限整数集”。
* **删除同步全集可见性**：删除文档即在新版本把对应 ``visible`` 置 0；
  posting 行也同步移除。因此被删文档不会再出现在任何查询结果中，
  NOT 也不会把它补回来（不可见即不属于全集）。
* 新版本由旧版本 **copy-on-commit** 复制而来（合成夹具规模下简单可靠），
  再在单个事务内应用 add/delete 变更，保证版本快照不可变。
* 读取接口返回排序去重的 ID 列表，交给算法层合并；
  **存储层不提供任何集合求交查询**，求交只能由核心算法完成。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..query.spec import ErrorCategory, QueryError

INITIAL_VERSION = 1


class StoreError(QueryError):
    """存储层失败的基类。"""

    category: ErrorCategory = ErrorCategory.INTERNAL


class VersionNotFoundError(StoreError):
    category = ErrorCategory.VERSION_NOT_FOUND


class VersionConflictError(StoreError):
    category = ErrorCategory.VERSION_CONFLICT


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VersionInfo:
    version_id: int
    parent_id: Optional[int]
    created_at: str
    message: str
    doc_count: int
    visible_count: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER REFERENCES versions(version_id),
    created_at TEXT NOT NULL,
    message    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS documents (
    version_id INTEGER NOT NULL REFERENCES versions(version_id),
    doc_id     INTEGER NOT NULL,
    visible    INTEGER NOT NULL CHECK (visible IN (0, 1)),
    PRIMARY KEY (version_id, doc_id)
);
CREATE TABLE IF NOT EXISTS postings (
    version_id INTEGER NOT NULL REFERENCES versions(version_id),
    term       TEXT NOT NULL,
    doc_id     INTEGER NOT NULL,
    PRIMARY KEY (version_id, term, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_postings_lookup
    ON postings (version_id, term, doc_id);
-- 词项目录：登记某版本“已知”的词项，与其 posting 是否为空解耦。
-- 因此删除全部文档后词项仍已知（空 posting），区别于从未索引过的未知词项。
CREATE TABLE IF NOT EXISTS term_directory (
    version_id INTEGER NOT NULL REFERENCES versions(version_id),
    term       TEXT NOT NULL,
    PRIMARY KEY (version_id, term)
);
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(version_id),
    kind       TEXT NOT NULL CHECK (kind IN ('add', 'delete', 'index')),
    doc_id     INTEGER,
    terms_json TEXT NOT NULL
);
"""


class VersionStore:
    """SQLite 版本存储。单连接 + 锁，适合本地/测试并发。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._ensure_initial_version()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _ensure_initial_version(self) -> None:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM versions")
        if cur.fetchone()["c"] == 0:
            self._conn.execute(
                "INSERT INTO versions (version_id, parent_id, created_at, message) VALUES (?, NULL, ?, ?)",
                (INITIAL_VERSION, _now(), "初始空版本：空全集"),
            )
            self._conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')"
            )
            self._conn.commit()

    # ---------------- 版本管理 ----------------

    def list_versions(self) -> List[VersionInfo]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT v.version_id, v.parent_id, v.created_at, v.message,
                       COUNT(d.doc_id) AS doc_count,
                       COALESCE(SUM(d.visible), 0) AS visible_count
                FROM versions v
                LEFT JOIN documents d ON d.version_id = v.version_id
                GROUP BY v.version_id
                ORDER BY v.version_id
                """
            ).fetchall()
        return [
            VersionInfo(
                version_id=r["version_id"],
                parent_id=r["parent_id"],
                created_at=r["created_at"],
                message=r["message"],
                doc_count=r["doc_count"],
                visible_count=r["visible_count"],
            )
            for r in rows
        ]

    def latest_version(self) -> int:
        versions = self.list_versions()
        return versions[-1].version_id

    def resolve_version(self, version: Optional[int]) -> int:
        """None / 'latest' 解析为最新版本；不存在则抛 VersionNotFoundError。"""
        latest = self.latest_version()
        if version is None:
            return latest
        return version

    def require_version(self, version: int) -> VersionInfo:
        infos = {v.version_id: v for v in self.list_versions()}
        if version not in infos:
            raise VersionNotFoundError(
                f"版本 {version} 不存在（现有版本：{sorted(infos)}）"
            )
        return infos[version]

    # ---------------- 读取 ----------------

    def universe(self, version: int) -> List[int]:
        """该版本的**可见**文档全集（NOT 的补集基准）。

        只包含 visible=1 的文档：删除即不可见，绝不参与求补。
        """
        self.require_version(version)
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id FROM documents WHERE version_id = ? AND visible = 1 ORDER BY doc_id",
                (version,),
            ).fetchall()
        return [r["doc_id"] for r in rows]

    def posting(self, version: int, term: str) -> List[int]:
        """读取某版本某词项的 posting；返回排序后的可见文档 ID。

        posting 表只对可见文档维护（删除时同步移除），
        这里再与 documents.visible 交集一次，作为双重防护。
        """
        self.require_version(version)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.doc_id AS doc_id
                FROM postings p
                JOIN documents d ON d.version_id = p.version_id AND d.doc_id = p.doc_id
                WHERE p.version_id = ? AND p.term = ? AND d.visible = 1
                ORDER BY p.doc_id
                """,
                (version, term),
            ).fetchall()
        return [r["doc_id"] for r in rows]

    def known_terms(self, version: int) -> List[str]:
        self.require_version(version)
        with self._lock:
            rows = self._conn.execute(
                "SELECT term FROM term_directory WHERE version_id = ? ORDER BY term",
                (version,),
            ).fetchall()
        return [r["term"] for r in rows]

    def bulk_postings(self, version: int, terms: Sequence[str]) -> Dict[str, List[int]]:
        return {t: self.posting(version, t) for t in terms}

    def events(self, version: int) -> List[dict]:
        self.require_version(version)
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, doc_id, terms_json FROM events "
                "WHERE version_id = ? ORDER BY seq",
                (version,),
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "kind": r["kind"],
                "doc_id": r["doc_id"],
                "terms": json.loads(r["terms_json"]),
            }
            for r in rows
        ]

    # ---------------- 写入（产生新版本） ----------------

    def commit(
        self,
        parent_id: Optional[int],
        adds: Iterable[Tuple[int, Sequence[str]]] = (),
        deletes: Iterable[int] = (),
        message: str = "",
    ) -> int:
        """在单个事务内基于父版本创建新版本并应用变更，返回新版本 ID。

        adds:    (doc_id, [term, ...]) —— 文档存在并为这些词项建索引
        deletes: [doc_id, ...]         —— 在新版本中删除（不可见）
        """
        adds = [(int(d), list(ts)) for d, ts in adds]
        deletes = [int(d) for d in deletes]

        with self._tx() as conn:
            if parent_id is None:
                parent_id = self.latest_version()
            else:
                self.require_version(parent_id)

            cur = conn.execute(
                "INSERT INTO versions (parent_id, created_at, message) VALUES (?, ?, ?)",
                (parent_id, _now(), message),
            )
            new_id = int(cur.lastrowid)

            # copy-on-commit：复制父版本的全集成员关系。
            conn.execute(
                "INSERT INTO documents (version_id, doc_id, visible) "
                "SELECT ?, doc_id, visible FROM documents WHERE version_id = ?",
                (new_id, parent_id),
            )
            # 复制父版本的 posting 索引。
            conn.execute(
                "INSERT INTO postings (version_id, term, doc_id) "
                "SELECT ?, term, doc_id FROM postings WHERE version_id = ?",
                (new_id, parent_id),
            )
            # 复制父版本的词项目录（空 posting 的词项也一并保留）。
            conn.execute(
                "INSERT INTO term_directory (version_id, term) "
                "SELECT ?, term FROM term_directory WHERE version_id = ?",
                (new_id, parent_id),
            )

            # 新增文档：进入全集（可见），写 posting，登记词项目录。
            for doc_id, terms in adds:
                conn.execute(
                    "INSERT INTO documents (version_id, doc_id, visible) "
                    "VALUES (?, ?, 1) "
                    "ON CONFLICT(version_id, doc_id) DO UPDATE SET visible = 1",
                    (new_id, doc_id),
                )
                for term in terms:
                    conn.execute(
                        "INSERT OR IGNORE INTO term_directory (version_id, term) "
                        "VALUES (?, ?)",
                        (new_id, term),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO postings (version_id, term, doc_id) VALUES (?, ?, ?)",
                        (new_id, term, doc_id),
                    )
                conn.execute(
                    "INSERT INTO events (version_id, kind, doc_id, terms_json) VALUES (?, 'index', ?, ?)",
                    (new_id, doc_id, json.dumps(sorted(terms))),
                )

            # 删除文档：同步全集可见性 + 移除 posting。
            for doc_id in deletes:
                row = conn.execute(
                    "SELECT visible FROM documents WHERE version_id = ? AND doc_id = ?",
                    (new_id, doc_id),
                ).fetchone()
                if row is None:
                    raise VersionConflictError(
                        f"不能删除版本 {new_id} 全集中不存在的文档 {doc_id}"
                    )
                conn.execute(
                    "UPDATE documents SET visible = 0 WHERE version_id = ? AND doc_id = ?",
                    (new_id, doc_id),
                )
                conn.execute(
                    "DELETE FROM postings WHERE version_id = ? AND doc_id = ?",
                    (new_id, doc_id),
                )
                conn.execute(
                    "INSERT INTO events (version_id, kind, doc_id, terms_json) "
                    "VALUES (?, 'delete', ?, '[]')",
                    (new_id, doc_id),
                )

            return new_id

    def reset(self) -> None:
        """清空全部数据并重建初始空版本（主要供测试与重新播种使用）。"""
        with self._tx() as conn:
            for tbl in ("events", "postings", "term_directory", "documents", "versions", "schema_meta"):
                conn.execute(f"DELETE FROM {tbl}")
            conn.execute(
                "INSERT INTO versions (version_id, parent_id, created_at, message) "
                "VALUES (?, NULL, ?, ?)",
                (INITIAL_VERSION, _now(), "初始空版本：空全集"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '1')"
            )
