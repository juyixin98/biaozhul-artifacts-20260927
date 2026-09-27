"""SQLite-backed job state for asynchronous measurements.

Status transitions are real (PENDING -> PROCESSING -> SUCCEEDED/FAILED),
persisted to a local file database. Results are stored verbatim as JSON so a
restarted worker can still answer GET /jobs/{id}.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

JOB_PENDING = "PENDING"
JOB_PROCESSING = "PROCESSING"
JOB_SUCCEEDED = "SUCCEEDED"
JOB_FAILED = "FAILED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL,
    status          TEXT NOT NULL,
    failure_code    TEXT,
    failure_message TEXT,
    input_label     TEXT,
    input_kind      TEXT,
    include_blocks  INTEGER NOT NULL DEFAULT 0,
    result_json     TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    worker_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_request_id ON jobs(request_id);
"""


class JobStore:
    def __init__(self, db_path: str, worker_id: str):
        self.db_path = db_path
        self.worker_id = worker_id
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def create(self, *, request_id: str, input_kind: str,
               include_blocks: bool, label: str | None) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, request_id, status, input_label, input_kind, "
                "include_blocks, created_at, updated_at, worker_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, request_id, JOB_PENDING, label, input_kind,
                 1 if include_blocks else 0, now, now, self.worker_id))
        return job_id

    def mark_processing(self, job_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                         (JOB_PROCESSING, time.time(), job_id))

    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, result_json = ?, updated_at = ? WHERE id = ?",
                (JOB_SUCCEEDED, json.dumps(result, ensure_ascii=False),
                 time.time(), job_id))

    def mark_failed(self, job_id: str, code: str, message: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, failure_code = ?, failure_message = ?, "
                "updated_at = ? WHERE id = ?",
                (JOB_FAILED, code, message, time.time(), job_id))

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        out = {
            "id": row["id"],
            "request_id": row["request_id"],
            "status": row["status"],
            "failure_code": row["failure_code"],
            "failure_message": row["failure_message"],
            "label": row["input_label"],
            "input_kind": row["input_kind"],
            "include_blocks": bool(row["include_blocks"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "worker_id": row["worker_id"],
        }
        if row["result_json"]:
            out["result"] = json.loads(row["result_json"])
        return out
