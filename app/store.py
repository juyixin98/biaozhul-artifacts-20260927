"""Persistent job state backed by SQLite.

A job is one analysis request (a fixture name or an uploaded trace). It has
two runs (adaptive, fixed). Ingest classifications and playout items are
stored so results are reproducible and inspectable after the request ends.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    created_ms  REAL NOT NULL,
    status      TEXT NOT NULL,
    source      TEXT NOT NULL,
    request_id  TEXT,
    client_ref  TEXT,
    config_json TEXT NOT NULL,
    summary_json TEXT,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    mode        TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (job_id, mode)
);
CREATE TABLE IF NOT EXISTS job_events (
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    seq         INTEGER NOT NULL,
    ts_ms       REAL NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT
);
"""


class JobStore:
    def __init__(self, path: str = ":memory:"):
        self._lock = threading.Lock()
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------- lifecycle
    def create_job(self, source: str, config: Dict[str, Any],
                   request_id: str, client_ref: Optional[str] = None) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(job_id, created_ms, status, source, "
                "request_id, client_ref, config_json) VALUES (?,?,?,?,?,?,?)",
                (job_id, time.time() * 1000.0, "PENDING", source, request_id,
                 client_ref, json.dumps(config, sort_keys=True)))
            self._add_event(job_id, 0, "JOB_CREATED",
                            f"source={source} request_id={request_id}")
            self._conn.commit()
        return job_id

    def mark_running(self, job_id: str) -> None:
        self._set_status(job_id, "RUNNING", "analysis started")

    def mark_completed(self, job_id: str, summary: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, summary_json=? WHERE job_id=?",
                ("COMPLETED", json.dumps(summary, sort_keys=True), job_id))
            self._add_event(job_id, time.time() * 1000.0, "JOB_COMPLETED", "")
            self._conn.commit()

    def mark_failed(self, job_id: str, error: str) -> None:
        self._set_status(job_id, "FAILED", error)

    def _set_status(self, job_id: str, status: str, detail: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, error=? WHERE job_id=?",
                (status, detail, job_id))
            self._add_event(job_id, time.time() * 1000.0, status, detail)
            self._conn.commit()

    # ------------------------------------------------------------- results
    def save_run(self, job_id: str, mode: str, result_dict: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(job_id, mode, result_json) "
                "VALUES (?,?,?)",
                (job_id, mode, json.dumps(result_dto(result_dict))))
            self._add_event(job_id, time.time() * 1000.0,
                            f"RUN_SAVED:{mode}",
                            f"playout_items={len(result_dict.get('playout', []))}")
            self._conn.commit()

    def get_run(self, job_id: str, mode: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM runs WHERE job_id=? AND mode=?",
                (job_id, mode)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        d["summary"] = json.loads(d.pop("summary_json")) if d["summary_json"] else None
        return d

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, created_ms, status, source, request_id, "
                "client_ref, error FROM jobs ORDER BY created_ms DESC "
                "LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_events(self, job_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts_ms, event, detail FROM job_events "
                "WHERE job_id=? ORDER BY seq", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def _add_event(self, job_id: str, ts_ms: float, event: str,
                   detail: str) -> None:
        nxt = self._conn.execute(
            "SELECT COALESCE(MAX(seq)+1,0) AS s FROM job_events "
            "WHERE job_id=?", (job_id,)).fetchone()["s"]
        self._conn.execute(
            "INSERT INTO job_events(job_id, seq, ts_ms, event, detail) "
            "VALUES (?,?,?,?,?)", (job_id, nxt, ts_ms, event, detail))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def result_dto(result: Dict[str, Any]) -> Dict[str, Any]:
    """Make an engine TraceResult dict JSON-safe (numpy scalars -> python)."""
    def fix(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: fix(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [fix(v) for v in o]
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.bool_):
            return bool(o)
        return o
    return fix(result)
