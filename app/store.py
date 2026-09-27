"""版本存储模块:SQLite 持久化文档版本与合并记录。

两张表:
- versions:文档某一方(base/local/remote)的一次快照,含规范化画像元数据。
- merges:一次合并的完整记录,含三方内容、状态、冲突、说明与(可选的)解决结果。

内容只存于数据库;诊断日志只写哈希指纹(见 app.diagnostics)。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .textnorm import profile

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('base', 'local', 'remote')),
    content TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    line_ending TEXT NOT NULL,
    ends_with_newline INTEGER NOT NULL,
    line_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merges (
    merge_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    document_id TEXT,
    base TEXT NOT NULL,
    local TEXT NOT NULL,
    remote TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    local_sha TEXT NOT NULL,
    remote_sha TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('clean', 'conflicted')),
    result_text TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    resolved_text TEXT,
    resolutions_json TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return profile(text).sha256


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------ versions

    def save_version(self, document_id: str, role: str, content: str) -> dict[str, Any]:
        if role not in ("base", "local", "remote"):
            raise ValueError(f"invalid role {role!r}")
        version_id = uuid.uuid4().hex
        p = profile(content)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    version_id, document_id, role, content, p.sha256,
                    p.line_ending, int(p.ends_with_newline), p.line_count, _now(),
                ),
            )
        return {
            "version_id": version_id,
            "document_id": document_id,
            "role": role,
            "sha256": p.sha256,
            "line_ending": p.line_ending,
            "ends_with_newline": p.ends_with_newline,
            "line_count": p.line_count,
        }

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM versions WHERE version_id = ?", (version_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_versions(self, document_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT version_id, document_id, role, sha256, line_ending,"
                " ends_with_newline, line_count, created_at"
                " FROM versions WHERE document_id = ? ORDER BY created_at",
                (document_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- merges

    def save_merge(
        self,
        *,
        request_id: str,
        document_id: str | None,
        base: str,
        local: str,
        remote: str,
        status: str,
        result_text: str,
        conflicts: list[dict[str, Any]],
        notes: list[str],
    ) -> str:
        merge_id = uuid.uuid4().hex
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO merges (merge_id, request_id, document_id, base, local,"
                " remote, base_sha, local_sha, remote_sha, status, result_text,"
                " conflicts_json, notes_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    merge_id, request_id, document_id, base, local, remote,
                    _sha(base), _sha(local), _sha(remote),
                    status, result_text,
                    json.dumps(conflicts, ensure_ascii=False),
                    json.dumps(notes, ensure_ascii=False),
                    _now(),
                ),
            )
        return merge_id

    def get_merge(self, merge_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM merges WHERE merge_id = ?", (merge_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_resolution(
        self, merge_id: str, resolved_text: str, choices: dict[int, str]
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE merges SET resolved_text = ?, resolutions_json = ?"
                " WHERE merge_id = ?",
                (
                    resolved_text,
                    json.dumps({str(k): v for k, v in choices.items()}, ensure_ascii=False),
                    merge_id,
                ),
            )
