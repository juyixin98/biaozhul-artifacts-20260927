"""作业状态持久化（SQLite）。

状态机：``queued → planning → succeeded(feasible/transcode_required)
| failed``。未知异常落 ``failed``，绝不伪装成成功；每个作业保存输入
快照（sources/container）与完整计划 JSON。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    status      TEXT NOT NULL,
    container   TEXT NOT NULL,
    sources     TEXT NOT NULL,
    plan_json   TEXT,
    error_code  TEXT,
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""

VALID_STATUS = {"queued", "planning", "succeeded", "failed"}


class JobStore:
    def __init__(self, db_path: str, run_id: str):
        self._run_id = run_id
        self._lock = threading.Lock()
        path = Path(db_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create(self, job_id: str, sources: list[str], container: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(job_id,run_id,status,container,sources,"
                "plan_json,error_code,error,created_at,updated_at) "
                "VALUES(?,?,?,?,?,NULL,NULL,NULL,?,?)",
                (job_id, self._run_id, "queued", container,
                 json.dumps(sources, ensure_ascii=False), now, now),
            )
            self._conn.commit()

    def update_status(self, job_id: str, status: str) -> None:
        if status not in VALID_STATUS:
            raise ValueError(f"非法状态 {status!r}")
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE job_id=?",
                (status, time.time(), job_id),
            )
            self._conn.commit()

    def save_plan(self, job_id: str, plan_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='succeeded', plan_json=?, updated_at=? WHERE job_id=?",
                (plan_json, time.time(), job_id),
            )
            self._conn.commit()

    def fail(self, job_id: str, error_code: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='failed', error_code=?, error=?, updated_at=? "
                "WHERE job_id=?",
                (error_code, message, time.time(), job_id),
            )
            self._conn.commit()

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id,run_id,status,container,error_code,created_at,updated_at "
                "FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
