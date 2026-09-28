"""SQLite-backed job state: every validation job plus its status transitions,
so outcomes (including failures) stay auditable after the request ends."""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    format TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    input_bytes INTEGER NOT NULL,
    options_json TEXT NOT NULL,
    run_id TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_job(self, *, job_id, fmt, content, options, run_id):
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, created_at, status, format, input_sha256,"
                " input_bytes, options_json, run_id) VALUES (?,?,?,?,?,?,?,?)",
                (job_id, _utcnow(), "PENDING", fmt, sha,
                 len(content.encode("utf-8")), json.dumps(options), run_id),
            )
            self._event(conn, job_id, "PENDING", "job created")

    def transition(self, job_id, status, detail=""):
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, job_id))
            self._event(conn, job_id, status, detail)

    def complete(self, job_id, result):
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET status=?, result_json=? WHERE job_id=?",
                         ("DONE", json.dumps(result, ensure_ascii=False), job_id))
            self._event(conn, job_id, "DONE",
                        f"repair_status={result['repair']['status']}")

    def fail(self, job_id, error):
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE jobs SET status=?, error_json=? WHERE job_id=?",
                         ("FAILED", json.dumps(error, ensure_ascii=False), job_id))
            self._event(conn, job_id, "FAILED", error.get("code", ""))

    def get(self, job_id):
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                return None
            events = conn.execute(
                "SELECT ts, status, detail FROM job_events WHERE job_id=? ORDER BY id",
                (job_id,)).fetchall()
        return {
            "job_id": row["job_id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "format": row["format"],
            "input_sha256": row["input_sha256"],
            "input_bytes": row["input_bytes"],
            "run_id": row["run_id"],
            "options": json.loads(row["options_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "events": [dict(e) for e in events],
        }

    @staticmethod
    def _event(conn, job_id, status, detail):
        conn.execute(
            "INSERT INTO job_events (job_id, ts, status, detail) VALUES (?,?,?,?)",
            (job_id, _utcnow(), status, detail),
        )
