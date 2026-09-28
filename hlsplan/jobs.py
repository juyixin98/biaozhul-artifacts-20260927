"""作业状态与版本存储（SQLite）。

两张表：
- versions：每个播放列表每次提交的解析快照（JSON），版本号按 name 自增；
- jobs：对比/计划等作业的状态机 PENDING -> DONE | FAILED，
  携带 request_id 便于诊断串联。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from .models import PlaylistSnapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    media_sequence INTEGER NOT NULL,
    discontinuity_sequence INTEGER NOT NULL,
    endlist INTEGER NOT NULL,
    target_duration REAL NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    request_id TEXT NOT NULL,
    input_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, db_path: str = ":memory:"):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- 版本 ----

    def save_version(self, snapshot: PlaylistSnapshot) -> int:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM versions WHERE name = ?",
                (snapshot.name,),
            ).fetchone()
            version = row["v"] + 1
            snapshot.version = version
            self._conn.execute(
                "INSERT INTO versions (name, version, media_sequence, "
                "discontinuity_sequence, endlist, target_duration, "
                "snapshot_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    snapshot.name,
                    version,
                    snapshot.media_sequence,
                    snapshot.discontinuity_sequence,
                    int(snapshot.endlist),
                    snapshot.target_duration,
                    json.dumps(snapshot.to_dict()),
                    _now(),
                ),
            )
        return version

    def get_version(self, name: str, version: int) -> Optional[PlaylistSnapshot]:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json FROM versions WHERE name = ? AND version = ?",
                (name, version),
            ).fetchone()
        if row is None:
            return None
        return PlaylistSnapshot.from_dict(json.loads(row["snapshot_json"]))

    def latest_version(self, name: str) -> Optional[PlaylistSnapshot]:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json FROM versions WHERE name = ? "
                "ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        if row is None:
            return None
        return PlaylistSnapshot.from_dict(json.loads(row["snapshot_json"]))

    # ---- 作业 ----

    def create_job(self, kind: str, request_id: str, input_data: dict) -> str:
        job_id = uuid.uuid4().hex[:16]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, kind, status, request_id, input_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, kind, "PENDING", request_id, json.dumps(input_data),
                 _now(), _now()),
            )
        return job_id

    def finish_job(
        self,
        job_id: str,
        status: str,
        result: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status = ?, result_json = ?, error = ?, "
                "updated_at = ? WHERE id = ?",
                (status,
                 json.dumps(result) if result is not None else None,
                 error, _now(), job_id),
            )

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "request_id": row["request_id"],
            "input": json.loads(row["input_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
