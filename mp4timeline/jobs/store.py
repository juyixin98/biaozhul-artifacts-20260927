"""SQLite 作业存储。

状态机：queued -> running -> done | failed
每次状态迁移、进度更新、日志行都落库，便于事后审计：
测试日志可凭 job_id / run_id 关联输入文件 sha256 与判定依据。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from ..errors import JobError, JobNotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    status      TEXT NOT NULL,
    input_path  TEXT NOT NULL,
    input_sha256 TEXT,
    progress    REAL NOT NULL DEFAULT 0.0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    result_json TEXT,
    error_class TEXT,
    error_detail TEXT
);
CREATE TABLE IF NOT EXISTS job_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    ts      REAL NOT NULL,
    message TEXT NOT NULL
);
"""

VALID_TRANSITIONS = {
    "queued": {"running", "failed"},
    "running": {"done", "failed"},
    "done": set(),
    "failed": set(),
}


class JobStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 写入 ----

    def create(self, input_path: str, run_id: str | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        run_id = run_id or uuid.uuid4().hex[:8]
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs(job_id, run_id, status, input_path, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (job_id, run_id, "queued", input_path, now, now),
            )
        self.log(job_id, f"作业创建 status=queued input={input_path} run_id={run_id}")
        return job_id

    def _transition(self, job_id: str, to: str, **fields) -> None:
        row = self.get(job_id)
        if row is None:
            raise JobNotFoundError(f"作业不存在: {job_id}")
        if to not in VALID_TRANSITIONS[row["status"]]:
            raise JobError(f"非法状态迁移 {row['status']} -> {to}")
        now = time.time()
        assignments = ["status = ?", "updated_at = ?"]
        values: list = [to, now]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(job_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?", values
            )
        self.log(job_id, f"status -> {to}")

    def start(self, job_id: str, input_sha256: str) -> None:
        self._transition(job_id, "running", input_sha256=input_sha256, progress=0.1)

    def set_progress(self, job_id: str, progress: float, message: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET progress = ?, updated_at = ? WHERE job_id = ?",
                (progress, time.time(), job_id),
            )
        self.log(job_id, f"progress={progress:.2f} {message}")

    def complete(self, job_id: str, result: dict) -> None:
        self._transition(
            job_id, "done", progress=1.0, result_json=json.dumps(result, ensure_ascii=False)
        )

    def fail(self, job_id: str, error_class: str, error_detail: str) -> None:
        self._transition(
            job_id, "failed", error_class=error_class, error_detail=error_detail
        )

    def log(self, job_id: str, message: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO job_logs(job_id, ts, message) VALUES (?,?,?)",
                (job_id, time.time(), message),
            )

    # ---- 读取 ----

    def get(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_or_raise(self, job_id: str) -> dict:
        row = self.get(job_id)
        if row is None:
            raise JobNotFoundError(f"作业不存在: {job_id}")
        return row

    def logs(self, job_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, message FROM job_logs WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def result(self, job_id: str) -> dict | None:
        row = self.get_or_raise(job_id)
        if row["result_json"] is None:
            return None
        return json.loads(row["result_json"])
