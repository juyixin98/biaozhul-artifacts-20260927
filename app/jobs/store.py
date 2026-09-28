"""SQLite-backed persistence for jobs, diagnostic events and reports."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    input_name   TEXT,
    input_size   INTEGER,
    input_sha256 TEXT,
    error        TEXT
);
CREATE TABLE IF NOT EXISTS events (
    job_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    code      TEXT NOT NULL,
    severity  TEXT NOT NULL,
    message   TEXT NOT NULL,
    pid       INTEGER,
    "offset"  INTEGER,
    context   TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS events_job_idx ON events(job_id, seq);
CREATE TABLE IF NOT EXISTS reports (
    job_id      TEXT PRIMARY KEY,
    report_json TEXT NOT NULL
);
"""

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class JobStore:
    def __init__(self, db_path: str):
        self._db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_job(
        self,
        job_id: str,
        created_at: str,
        input_name: Optional[str],
        input_size: int,
        input_sha256: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(job_id, status, created_at, updated_at,"
                " input_name, input_size, input_sha256, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (job_id, STATUS_QUEUED, created_at, created_at,
                 input_name, input_size, input_sha256),
            )
            self._conn.commit()

    def set_status(
        self, job_id: str, status: str, updated_at: str, error: Optional[str] = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, error = ? WHERE job_id = ?",
                (status, updated_at, error, job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, status, created_at, updated_at, input_name,"
                " input_size, input_sha256, error FROM jobs"
                " ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_events(self, job_id: str, events: list) -> None:
        rows = [
            (
                job_id,
                event.seq,
                event.code,
                event.severity,
                event.message,
                event.pid,
                event.offset,
                json.dumps(event.context, ensure_ascii=False),
            )
            for event in events
        ]
        with self._lock:
            self._conn.executemany(
                'INSERT INTO events(job_id, seq, code, severity, message, pid,'
                ' "offset", context) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                rows,
            )
            self._conn.commit()

    def get_events(
        self, job_id: str, limit: int = 200, offset: int = 0
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                'SELECT seq, code, severity, message, pid, "offset", context'
                " FROM events WHERE job_id = ? ORDER BY seq LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["context"] = json.loads(item["context"])
            out.append(item)
        return out

    def save_report(self, job_id: str, report: dict) -> None:
        payload = json.dumps(report, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO reports(job_id, report_json) VALUES (?, ?)"
                " ON CONFLICT(job_id) DO UPDATE SET report_json = excluded.report_json",
                (job_id, payload),
            )
            self._conn.commit()

    def get_report(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT report_json FROM reports WHERE job_id = ?", (job_id,)
            ).fetchone()
        return json.loads(row["report_json"]) if row is not None else None

    def purge_oldest(self, keep: int) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id FROM jobs ORDER BY created_at ASC"
            ).fetchall()
            remove = [r["job_id"] for r in rows[:-keep]] if len(rows) > keep else []
            for job_id in remove:
                self._conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
                self._conn.execute("DELETE FROM reports WHERE job_id = ?", (job_id,))
                self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            self._conn.commit()
        return len(remove)
