"""SQLite-backed job store.

Each alignment is a job with a stable lifecycle. Status transitions and the
key processing step that produced them are kept in an ``events`` table so a
status page can show *what happened and where*, not just a final string.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import StorageError
from .logging_setup import get_logger

log = get_logger("storage")

TERMINAL_STATES = {"succeeded", "failed", "rejected_insufficient_evidence"}
ACTIVE_STATES = {"queued", "running"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    status        TEXT NOT NULL,
    submitted_at  REAL NOT NULL,
    updated_at    REAL NOT NULL,
    request       TEXT NOT NULL,
    failure       TEXT,
    result_path   TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    ts        REAL NOT NULL,
    seq       INTEGER NOT NULL,
    step      TEXT NOT NULL,
    status    TEXT NOT NULL,
    detail    TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, seq);
CREATE INDEX IF NOT EXISTS idx_jobs_request ON jobs(request_id);
"""


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_job(self, request_id: str, request: dict) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(job_id, request_id, status, submitted_at, "
                "updated_at, request) VALUES (?, ?, 'queued', ?, ?, ?)",
                (job_id, request_id, now, now, json.dumps(request)))
            self._conn.execute(
                "INSERT INTO events(job_id, ts, seq, step, status, detail) "
                "VALUES (?, ?, 0, 'job.queued', 'ok', ?)",
                (job_id, now, json.dumps({"request_id": request_id})))
            self._conn.commit()
        log.info("job created", extra={"fields": {"job_id": job_id,
                                                   "request_id": request_id}})
        return job_id

    def set_status(self, job_id: str, status: str, *, failure: dict | None = None,
                   result_path: str | None = None) -> None:
        with self._lock:
            cur = self._conn.execute("SELECT status FROM jobs WHERE job_id=?",
                                     (job_id,))
            row = cur.fetchone()
            if row is None:
                raise StorageError(f"unknown job {job_id}")
            self._conn.execute(
                "UPDATE jobs SET status=?, updated_at=?, failure=COALESCE(?, "
                "failure), result_path=COALESCE(?, result_path) WHERE job_id=?",
                (status, time.time(),
                 json.dumps(failure) if failure is not None else None,
                 result_path, job_id))
            self._conn.commit()

    def add_event(self, job_id: str, step: str, status: str,
                  detail: dict | None = None) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM events "
                "WHERE job_id=?", (job_id,)).fetchone()
            self._conn.execute(
                "INSERT INTO events(job_id, ts, seq, step, status, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, time.time(), int(row["next_seq"]), step, status,
                 json.dumps(detail or {})))
            self._conn.commit()

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?",
                                     (job_id,)).fetchone()
            if row is None:
                return {}
            events = self._conn.execute(
                "SELECT ts, seq, step, status, detail FROM events WHERE job_id=? "
                "ORDER BY seq", (job_id,)).fetchall()
        return {
            "job_id": row["job_id"],
            "request_id": row["request_id"],
            "status": row["status"],
            "submitted_at": row["submitted_at"],
            "updated_at": row["updated_at"],
            "request": json.loads(row["request"]),
            "failure": json.loads(row["failure"]) if row["failure"] else None,
            "result_path": row["result_path"],
            "events": [{"ts": e["ts"], "seq": e["seq"], "step": e["step"],
                        "status": e["status"],
                        "detail": json.loads(e["detail"] or "{}")}
                       for e in events],
        }

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, request_id, status, submitted_at, updated_at "
                "FROM jobs ORDER BY submitted_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
