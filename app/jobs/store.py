"""Job state layer — SQLite-backed persistence for validation jobs.

Status machine (explicit; unknown states are never reported as success):

    queued -> running -> succeeded
                       -> failed (solver infeasible / budget / too large)
                       -> parse_failed
    queued/running -> error (unexpected exception, message stored)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from ..logging_setup import get_logger

log = get_logger("jobs")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    fmt               TEXT,
    cue_count         INTEGER NOT NULL DEFAULT 0,
    input_bytes       BLOB NOT NULL,
    input_sha256      TEXT NOT NULL,
    result_json       TEXT,
    repaired_document TEXT,
    error_message     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""

_TERMINAL = {"succeeded", "failed", "parse_failed", "error"}


class JobStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def create(self, job_id: str, data: bytes, fmt: str | None,
               input_sha256: str) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (job_id, status, fmt, input_bytes, "
                "input_sha256, created_at, updated_at) "
                "VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                (job_id, fmt, data, input_sha256, now, now),
            )
        log.info("[%s] job created (%d bytes, fmt=%s)", job_id, len(data), fmt)

    def set_running(self, job_id: str) -> None:
        self._update(job_id, status="running")

    def finish(self, job_id: str, *, status: str, fmt: str, cue_count: int,
               result: dict[str, Any], repaired_document: str | None) -> None:
        if status not in _TERMINAL:
            raise ValueError(f"terminal status required, got {status!r}")
        self._update(
            job_id,
            status=status,
            fmt=fmt,
            cue_count=cue_count,
            result_json=json.dumps(result, ensure_ascii=False),
            repaired_document=repaired_document,
        )

    def fail_exception(self, job_id: str, message: str) -> None:
        self._update(job_id, status="error", error_message=message)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d.pop("input_bytes", None)  # don't echo payload in listings
        if d.get("result_json"):
            d["result"] = json.loads(d["result_json"])
        d.pop("result_json", None)
        return d

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, status, fmt, cue_count, input_sha256, "
                "error_message, created_at, updated_at "
                "FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = [*fields.values(), job_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ?", values
            )
            if cur.rowcount == 0:
                raise KeyError(f"unknown job {job_id}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
