"""SQLite-backed job state store.

Jobs move through queued -> analysing -> completed | failed.  Every
transition and every pipeline step is appended to ``job_events`` so the
progress of a run is inspectable after the fact.  Nothing outside this
module touches the database.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import dependency_versions

STATUS_QUEUED = "queued"
STATUS_ANALYSING = "analysing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    request_json TEXT NOT NULL,
    plan_json TEXT,
    error_category TEXT,
    error_detail TEXT,
    app_version TEXT NOT NULL,
    versions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    step TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- jobs ----------------------------------------------------------------
    def create_job(self, run_id: str, request: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex[:16]
        versions = dependency_versions()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (job_id, run_id, status, request_json,"
                " app_version, versions_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (job_id, run_id, STATUS_QUEUED, json.dumps(request),
                 versions["app"], json.dumps(versions), _now(), _now()))
        self.add_event(job_id, "queued", "INFO", "job accepted",
                       {"request": request})
        return job_id

    def _update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE jobs SET {cols} WHERE job_id = ?",
                (*fields.values(), job_id))

    def mark_analysing(self, job_id: str) -> None:
        self._update(job_id, status=STATUS_ANALYSING)
        self.add_event(job_id, "analysing", "INFO", "analysis started", None)

    def complete_job(self, job_id: str, decision: str, plan: dict[str, Any],
                     reasons: list[dict[str, Any]]) -> None:
        self._update(job_id, status=STATUS_COMPLETED, decision=decision,
                     plan_json=json.dumps(plan))
        self.add_event(job_id, "completed", "INFO",
                       f"job completed with decision {decision}",
                       {"decision": decision, "reasons": reasons})

    def attach_plan(self, job_id: str, plan: dict[str, Any]) -> None:
        """Persist a plan on a failed job so it can be inspected."""
        self._update(job_id, plan_json=json.dumps(plan))

    def fail_job(self, job_id: str, category: str, detail: str) -> None:
        self._update(job_id, status=STATUS_FAILED, decision="failed",
                     error_category=category, error_detail=detail)
        self.add_event(job_id, "failed", "ERROR",
                       f"job failed: {category}", {"detail": detail})

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        job["request"] = json.loads(job.pop("request_json"))
        job["versions"] = json.loads(job.pop("versions_json"))
        if job.get("plan_json"):
            job["plan"] = json.loads(job["plan_json"])
        job.pop("plan_json", None)
        return job

    # -- events ----------------------------------------------------------------
    def add_event(self, job_id: str, step: str, level: str, message: str,
                  data: dict[str, Any] | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO job_events (job_id, ts, step, level, message,"
                " data_json) VALUES (?,?,?,?,?,?)",
                (job_id, _now(), step, level, message,
                 json.dumps(data) if data is not None else None))

    def get_events(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, step, level, message, data_json FROM job_events"
                " WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            if event["data_json"] is not None:
                event["data"] = json.loads(event["data_json"])
            event.pop("data_json", None)
            events.append(event)
        return events
