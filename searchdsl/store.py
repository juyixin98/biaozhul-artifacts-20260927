"""SQLite 版本存储。

- documents：文档带单调递增 version，重复 upsert 版本号 +1；
- postings：倒排表（含位置），由 index 模块写入；
- query_versions：规范查询树内容寻址（sha256[:16]），
  同一棵规范树永远得到同一个版本号，记录首次出现与运行次数。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterable, List, Optional

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id     TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    author     TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    year       INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postings (
    field     TEXT NOT NULL,
    term      TEXT NOT NULL,
    doc_id    TEXT NOT NULL,
    positions TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (field, term, doc_id)
);
CREATE TABLE IF NOT EXISTS query_versions (
    version_hash   TEXT PRIMARY KEY,
    canonical_json TEXT NOT NULL,
    first_seen     TEXT NOT NULL,
    run_count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_postings_doc ON postings(doc_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI TestClient/服务线程与建连线程不同；SQLite 写操作由引擎串行发起
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- 文档 -------------------------------------------------------------
    def reset_documents(self, documents: Iterable[dict]) -> None:
        """全量重建文档（旧版本号清零），调用方随后重建索引。"""
        self.conn.execute("DELETE FROM postings")
        self.conn.execute("DELETE FROM documents")
        now = utc_now()
        for doc in documents:
            self.conn.execute(
                "INSERT INTO documents(doc_id, version, title, body, author, tags, year, updated_at)"
                " VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                (doc["id"], doc.get("title", ""), doc.get("body", ""),
                 doc.get("author", ""), doc.get("tags", ""), doc.get("year"), now),
            )
        self.conn.commit()

    def upsert_document(self, doc: dict) -> int:
        """单文档 upsert：已存在则版本号 +1，返回新版本号。"""
        row = self.conn.execute(
            "SELECT version FROM documents WHERE doc_id = ?", (doc["id"],)
        ).fetchone()
        new_version = 1 if row is None else int(row["version"]) + 1
        self.conn.execute(
            "INSERT INTO documents(doc_id, version, title, body, author, tags, year, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(doc_id) DO UPDATE SET"
            " version=excluded.version, title=excluded.title, body=excluded.body,"
            " author=excluded.author, tags=excluded.tags, year=excluded.year, updated_at=excluded.updated_at",
            (doc["id"], new_version, doc.get("title", ""), doc.get("body", ""),
             doc.get("author", ""), doc.get("tags", ""), doc.get("year"), utc_now()),
        )
        self.conn.commit()
        return new_version

    def get_document(self, doc_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()

    def all_documents(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM documents ORDER BY doc_id"))

    def all_doc_ids(self) -> List[str]:
        return [r["doc_id"] for r in self.conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")]

    # -- 倒排表 -----------------------------------------------------------
    def replace_postings(self, rows: Iterable[tuple]) -> None:
        self.conn.execute("DELETE FROM postings")
        self.conn.executemany(
            "INSERT OR REPLACE INTO postings(field, term, doc_id, positions)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def posting_docs(self, field: str, term: str) -> List[str]:
        return [r["doc_id"] for r in self.conn.execute(
            "SELECT doc_id FROM postings WHERE field = ? AND term = ?", (field, term)
        )]

    def posting_positions(self, field: str, term: str) -> dict[str, List[int]]:
        out: dict[str, List[int]] = {}
        for r in self.conn.execute(
            "SELECT doc_id, positions FROM postings WHERE field = ? AND term = ?",
            (field, term),
        ):
            out[r["doc_id"]] = json.loads(r["positions"])
        return out

    # -- 查询版本 ---------------------------------------------------------
    @staticmethod
    def canonical_hash(canonical: dict) -> tuple[str, str]:
        canonical_json = json.dumps(
            canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return sha256(canonical_json.encode("utf-8")).hexdigest()[:16], canonical_json

    def register_query(self, canonical: dict) -> str:
        h, canonical_json = self.canonical_hash(canonical)
        self.conn.execute(
            "INSERT INTO query_versions(version_hash, canonical_json, first_seen, run_count)"
            " VALUES (?, ?, ?, 1)"
            " ON CONFLICT(version_hash) DO UPDATE SET run_count = run_count + 1",
            (h, canonical_json, utc_now()),
        )
        self.conn.commit()
        return h

    def get_query_version(self, version_hash: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM query_versions WHERE version_hash = ?", (version_hash,)
        ).fetchone()
        return dict(row) if row else None
