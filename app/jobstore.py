"""SQLite-backed job store.

Only metadata/results live in SQLite (id, format, state, request identity,
result JSON, error category, timings). The audio bytes themselves are never
stored; the in-process streaming meter consumes each chunk and discards it.
The store is intentionally synchronous (sqlite3 module) which is fine for the
local single-worker usage described in the README.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    channels        INTEGER NOT NULL,
    sample_format   TEXT NOT NULL,
    roles_json      TEXT NOT NULL,
    request_id      TEXT,
    result_json     TEXT,
    error_code      TEXT,
    error_message   TEXT,
    bytes_received  INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    finalized_at    REAL,
    processing_ms   REAL
);
"""

# Job state machine: OPEN -> FINALIZED, or OPEN -> FAILED at any point.
STATE_OPEN = "OPEN"
STATE_FINALIZED = "FINALIZED"
STATE_FAILED = "FAILED"


class JobStore:
    def __init__(self, path: str = ":memory:"):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.executescript(SCHEMA)

    def create(self, *, channels: int, sample_format: str, roles: list[str],
               request_id: str | None) -> str:
        job_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, state, channels, sample_format, "
                "roles_json, request_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, STATE_OPEN, channels, sample_format,
                 json.dumps(roles), request_id, time.time()),
            )
        return job_id

    def add_bytes(self, job_id: str, n: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET bytes_received = bytes_received + ? WHERE id=?",
                (n, job_id),
            )

    def get(self, job_id: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
            return cur.fetchone()

    def finalize(self, job_id: str, result: dict[str, Any],
                 processing_ms: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET state=?, result_json=?, finalized_at=?, "
                "processing_ms=? WHERE id=?",
                (STATE_FINALIZED, json.dumps(result), time.time(),
                 processing_ms, job_id),
            )

    def fail(self, job_id: str, code: str, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET state=?, error_code=?, error_message=?, "
                "finalized_at=? WHERE id=?",
                (STATE_FAILED, code, message, time.time(), job_id),
            )

    def list_recent(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            return list(cur.fetchall())

    def close(self) -> None:
        with self._lock:
            self._conn.close()
