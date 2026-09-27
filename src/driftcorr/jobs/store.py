"""SQLite job store. One connection per call keeps it thread-safe enough for
the synchronous API worker; WAL mode allows concurrent reads during a write.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import PIPELINE_VERSION
from .models import JobRecord, JobStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    params_json TEXT NOT NULL,
    result_json TEXT,
    error_class TEXT,
    error_message TEXT,
    pipeline_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_request_id ON jobs(request_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def create(self, *, request_id: str, params: dict[str, Any]) -> JobRecord:
        job_id = uuid.uuid4().hex[:12]
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, request_id, status, created_at, "
                "updated_at, params_json, pipeline_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, request_id, JobStatus.PENDING.value, now, now,
                 json.dumps(params), PIPELINE_VERSION),
            )
        return self.get(job_id)  # type: ignore[return-value]

    def _update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {cols} WHERE job_id = ?",
                (*fields.values(), job_id),
            )

    def mark_running(self, job_id: str) -> None:
        self._update(job_id, status=JobStatus.RUNNING.value)

    def save_result(self, job_id: str, result: dict[str, Any]) -> None:
        status = JobStatus.DONE if result.get("status") != "failed" else JobStatus.FAILED
        failure = result.get("failure") or {}
        self._update(
            job_id,
            status=status.value,
            result_json=json.dumps(result),
            error_class=failure.get("error_class"),
            error_message=failure.get("message"),
        )

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._to_record(row) if row else None

    def list(self, limit: int = 50) -> list[JobRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_record(r) for r in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            request_id=row["request_id"],
            status=JobStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            params=json.loads(row["params_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error_class=row["error_class"],
            error_message=row["error_message"],
            pipeline_version=row["pipeline_version"],
        )
