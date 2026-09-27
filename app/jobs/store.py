"""SQLite 作业存储：状态机与结果持久化。

状态机：``queued -> running -> succeeded | failed``。
作业结果（场景报告/计划）以 JSON 整存，避免对核心模型做侵入式 ORM。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_us  INTEGER NOT NULL,
    updated_us  INTEGER NOT NULL,
    request_id  TEXT,
    error       TEXT,
    payload_json TEXT,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


class JobStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(jobs)").fetchall()}
            if "payload_json" not in cols:
                # 兼容早期空库：补列（本项目从空目录起步，无历史数据需迁移）
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN payload_json TEXT")

    def create(self, job_id: str, kind: str, request_id: str,
               payload: dict[str, Any]) -> None:
        now = self._now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs(job_id, kind, status, created_us, updated_us,"
                " request_id, error, payload_json, result_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, kind, "queued", now, now, request_id, None,
                 json.dumps(payload, ensure_ascii=False), None))

    def set_status(self, job_id: str, status: str,
                   error: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status=?, updated_us=?, error=? WHERE job_id=?",
                (status, self._now(), error, job_id))

    def save_result(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status='succeeded', updated_us=?, "
                "result_json=? WHERE job_id=?",
                (self._now(), json.dumps(result, ensure_ascii=False), job_id))

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["result"] = json.loads(d["result_json"]) if d["result_json"] else None
        d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else None
        d.pop("result_json", None)
        d.pop("payload_json", None)
        return d

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT job_id, kind, status, created_us, updated_us, "
                "request_id, error FROM jobs ORDER BY created_us DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1_000_000)
