"""SQLite 版本存储：文档、追加事件日志、版本化全集快照与持久化 posting 块。

版本模型
--------
- 版本号从 1 开始单调递增；版本 0 是“空全集”基线，永不在事件日志中出现。
- 每次 commit 追加一批事件（``add`` / ``delete``），生成新版本，并**物化**
  该版本的可见文档全集快照（universe_snapshot）。NOT 只相对于此显式有限全集求补。
- ``add``：若文档此前被删除则恢复（active=1）；新增文档写入正文。
  被删除文档的正文保留在 documents 表中以便审计/恢复，但不可见。
- ``delete``：只同步全集可见性（active=0 + 快照移除）。
  **posting 块不做重写**——块上界只用于安全跳过，可见性过滤交给查询核心，
  因此删除后块上界依然安全（可见集合是已索引集合的子集）。
- postings 表是规范化 posting 行；blocks 表是按 block_size 重建的持久跳跃索引。

集合运算**不**在 SQL 中进行：查询核心从本层取出已排序的 PostingList，
交集/并集/补集全部由 app.index.posting 的游标算法完成（测试中会强制检查）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from ..index.posting import Block, PostingList
from .encoding import decode_block, encode_ids

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS events (
    version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('add', 'delete')),
    doc_id INTEGER NOT NULL,
    PRIMARY KEY (version, seq)
);
CREATE TABLE IF NOT EXISTS universe_members (
    version INTEGER NOT NULL,
    doc_id INTEGER NOT NULL,
    PRIMARY KEY (version, doc_id)
);
CREATE TABLE IF NOT EXISTS postings (
    term TEXT NOT NULL,
    doc_id INTEGER NOT NULL,
    PRIMARY KEY (term, doc_id)
);
CREATE TABLE IF NOT EXISTS blocks (
    term TEXT NOT NULL,
    block_idx INTEGER NOT NULL,
    upper INTEGER NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY (term, block_idx)
);
CREATE INDEX IF NOT EXISTS idx_postings_term ON postings(term);
CREATE INDEX IF NOT EXISTS idx_blocks_term_upper ON blocks(term, upper);
CREATE TABLE IF NOT EXISTS request_traces (
    request_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    version INTEGER,
    expression TEXT,
    status TEXT NOT NULL,
    error_category TEXT,
    error_message TEXT,
    result_count INTEGER,
    stats_json TEXT,
    steps_json TEXT
);
"""


class StorageError(RuntimeError):
    """存储层失败（类别统一为 storage_error）。"""


class VersionStore:
    """封装一个 SQLite 文件。线程安全：所有连接访问由内部锁串行化。"""

    def __init__(self, db_path: str, block_size: int = 8) -> None:
        self.db_path = db_path
        self.block_size = block_size
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(SCHEMA)
            cur = self._conn.execute(
                "SELECT value FROM meta WHERE key='block_size'"
            ).fetchone()
            if cur is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('block_size', ?)",
                    (str(block_size),),
                )
            else:
                self.block_size = int(cur["value"])

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

    # ------------------------------------------------------------------
    # 版本与文档写入
    # ------------------------------------------------------------------

    def latest_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM events"
            ).fetchone()
            return int(row["v"])

    def version_exists(self, version: int) -> bool:
        with self._lock:
            # 版本由事件定义；即使该版本全集恰好为空（universe_members 无行），
            # 版本依然存在（NOT 在空全集上必须可查询）。
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE version=? LIMIT 1", (version,)
            ).fetchone()
            return row is not None or version == 0

    def resolve_version(self, version: int | None) -> int:
        """None -> 最新版本；显式版本不存在抛 StorageError。"""
        if version is None:
            return self.latest_version()
        if not self.version_exists(version):
            raise StorageError(f"版本 {version} 不存在")
        return version

    def commit(
        self,
        adds: dict[int, str] | None = None,
        deletes: list[int] | None = None,
        note: str | None = None,
    ) -> int:
        """原子提交一批 add/delete 事件，返回新版本号。

        - adds: {doc_id: text}，文档必须已分词为 term（这里对正文分词）；
        - deletes: 要删除（隐藏）的文档 ID 列表；
        - 删除一个不存在/不可见的文档属于幂等无操作，不报错。
        """
        from ..text.tokenize import unique_terms

        adds = adds or {}
        deletes = list(deletes or [])
        if not adds and not deletes:
            raise StorageError("空提交：至少需要一个 add 或 delete 事件")
        with self._tx() as conn:
            prev_version = self.latest_version()
            version = prev_version + 1
            # 当前可见全集
            visible = set(
                r["doc_id"]
                for r in conn.execute(
                    "SELECT doc_id FROM universe_members WHERE version=?",
                    (prev_version,),
                )
            )
            seq = 0
            affected_terms: set[str] = set()

            for doc_id, text in adds.items():
                if not isinstance(doc_id, int) or isinstance(doc_id, bool) or doc_id < 0:
                    raise StorageError(f"非法文档 ID：{doc_id!r}")
                if not isinstance(text, str):
                    raise StorageError(f"文档 {doc_id} 的正文必须是字符串")
                conn.execute(
                    "INSERT INTO documents(id, text, active) VALUES (?,?,1) "
                    "ON CONFLICT(id) DO UPDATE SET text=excluded.text, active=1",
                    (doc_id, text),
                )
                conn.execute(
                    "INSERT INTO events(version, seq, op, doc_id) VALUES (?,?,?,?)",
                    (version, seq, "add", doc_id),
                )
                seq += 1
                visible.add(doc_id)
                # 重建该文档涉及的 term posting（删除旧行后重插，保证幂等重加）
                terms = unique_terms(text)
                placeholders = ",".join("?" for _ in terms)
                if terms:
                    conn.execute(
                        f"DELETE FROM postings WHERE term IN ({placeholders}) "
                        f"AND doc_id=?",
                        (*terms, doc_id),
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO postings(term, doc_id) VALUES (?,?)",
                        [(t, doc_id) for t in terms],
                    )
                affected_terms.update(terms)

            for doc_id in deletes:
                if not isinstance(doc_id, int) or isinstance(doc_id, bool) or doc_id < 0:
                    raise StorageError(f"非法文档 ID：{doc_id!r}")
                conn.execute(
                    "INSERT INTO events(version, seq, op, doc_id) VALUES (?,?,?,?)",
                    (version, seq, "delete", doc_id),
                )
                seq += 1
                visible.discard(doc_id)
                # 关键：删除只同步全集可见性，posting/blocks 不重写
                conn.execute(
                    "UPDATE documents SET active=0 WHERE id=?", (doc_id,)
                )

            # 物化新版本的全集快照（显式有限全集，NOT 的补集基准）
            conn.executemany(
                "INSERT INTO universe_members(version, doc_id) VALUES (?,?)",
                [(version, d) for d in sorted(visible)],
            )
            if note is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                    (f"note:v{version}", note),
                )
            self._rebuild_blocks(conn, affected_terms)
            return version

    def _rebuild_blocks(self, conn: sqlite3.Connection, terms) -> None:
        """为受影响的 term 从 postings 重建持久化跳跃块。"""
        for term in sorted(terms):
            ids = tuple(
                r["doc_id"]
                for r in conn.execute(
                    "SELECT doc_id FROM postings WHERE term=? ORDER BY doc_id",
                    (term,),
                )
            )
            conn.execute("DELETE FROM blocks WHERE term=?", (term,))
            if not ids:
                continue
            rows = []
            for idx, i in enumerate(range(0, len(ids), self.block_size)):
                chunk = ids[i : i + self.block_size]
                rows.append((term, idx, chunk[-1], encode_ids(chunk)))
            conn.executemany(
                "INSERT INTO blocks(term, block_idx, upper, payload) "
                "VALUES (?,?,?,?)",
                rows,
            )

    # ------------------------------------------------------------------
    # 读取路径
    # ------------------------------------------------------------------

    def universe(self, version: int) -> PostingList:
        """取某版本物化的可见全集（版本 0 为空全集）。"""
        version = self.resolve_version(version)
        with self._lock:
            ids = tuple(
                r["doc_id"]
                for r in self._conn.execute(
                    "SELECT doc_id FROM universe_members WHERE version=? ORDER BY doc_id",
                    (version,),
                )
            )
        return PostingList.from_sorted(ids, self.block_size)

    def posting(self, term: str) -> PostingList:
        """取 term 的持久 posting 列表（含曾删除文档——由可见全集在查询时过滤）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM blocks WHERE term=? ORDER BY block_idx", (term,)
            ).fetchall()
        blocks = tuple(decode_block(r["payload"]) for r in rows)
        return PostingList.from_blocks(blocks, self.block_size)

    def term_exists(self, term: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM postings WHERE term=? LIMIT 1", (term,)
                ).fetchone()
                is not None
            )

    def list_terms(self, prefix: str | None = None, limit: int = 200) -> list[str]:
        with self._lock:
            if prefix is None:
                rows = self._conn.execute(
                    "SELECT DISTINCT term FROM postings ORDER BY term LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT DISTINCT term FROM postings WHERE term LIKE ? "
                    "ORDER BY term LIMIT ?",
                    (prefix.replace("\\", "\\\\").replace("%", "\\%") + "%", limit),
                ).fetchall()
        return [r["term"] for r in rows]

    def document(self, doc_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, text, active FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
        return dict(row) if row else None

    def versions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, COUNT(*) AS event_count FROM events "
                "GROUP BY version ORDER BY version"
            ).fetchall()
            sizes = {
                r["version"]: r["n"]
                for r in self._conn.execute(
                    "SELECT version, COUNT(*) AS n FROM universe_members GROUP BY version"
                )
            }
        out = [{"version": 0, "event_count": 0, "universe_size": 0}]
        for r in rows:
            v = r["version"]
            out.append(
                {
                    "version": v,
                    "event_count": r["event_count"],
                    "universe_size": sizes.get(v, 0),
                }
            )
        return out

    # ------------------------------------------------------------------
    # 请求诊断记录
    # ------------------------------------------------------------------

    def save_trace(self, trace: dict) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO request_traces"
                "(request_id, started_at, finished_at, version, expression, status, "
                " error_category, error_message, result_count, stats_json, steps_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trace["request_id"],
                    trace.get("started_at"),
                    trace.get("finished_at"),
                    trace.get("version"),
                    trace.get("expression"),
                    trace.get("status", "ok"),
                    trace.get("error_category"),
                    trace.get("error_message"),
                    trace.get("result_count"),
                    json.dumps(trace.get("stats"), ensure_ascii=False),
                    json.dumps(trace.get("steps"), ensure_ascii=False),
                ),
            )

    def get_trace(self, request_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM request_traces WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["stats"] = json.loads(d["stats_json"]) if d["stats_json"] else None
        d["steps"] = json.loads(d["steps_json"]) if d["steps_json"] else None
        del d["stats_json"]
        del d["steps_json"]
        return d

    def recent_traces(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT request_id, started_at, version, expression, status, "
                "error_category, result_count FROM request_traces "
                "ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
