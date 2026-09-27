"""SQLite-backed asynchronous job state.

A single background worker drains queued jobs; SQLite (WAL mode) is the
durable state store.  The processing itself is pure parsing and needs no
external services; the queue exists so large uploads return immediately with
a job id that can be polled.
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    request_id    TEXT NOT NULL,
    status        TEXT NOT NULL,              -- queued|running|done|failed
    submitted_at  REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    input_name    TEXT,
    input_bytes   INTEGER NOT NULL,
    error         TEXT,
    report        TEXT                         -- JSON when status=done
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class JobManager:
    def __init__(self, settings: Settings,
                 processor: Callable[[bytes, str], dict[str, Any]] | None = None,
                 start_worker: bool = True) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            settings.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._conn:
            self._conn.executescript(SCHEMA)

        self._processor = processor
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        if start_worker:
            self.start_worker_thread()

    # ------------------------------------------------------------------
    def start_worker_thread(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run_worker, name="mtsa-job-worker", daemon=True)
        self._worker.start()

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._process(job_id)
            except Exception as exc:  # pragma: no cover - defensive
                self._mark_failed(job_id, repr(exc))
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------
    def submit(self, data: bytes, request_id: str,
               input_name: str | None = None) -> str:
        job_id = uuid.uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs(job_id, request_id, status, submitted_at, "
                "input_name, input_bytes) VALUES (?,?,?,?,?,?)",
                (job_id, request_id, STATUS_QUEUED, time.time(),
                 input_name, len(data)))
        # The payload is handed to the worker in-process; for a single-node
        # local backend this avoids persisting raw input into the database.
        self._payloads: dict[str, bytes] = getattr(self, "_payloads", {})
        self._payloads[job_id] = data
        self._queue.put(job_id)
        return job_id

    def _process(self, job_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status=?, started_at=? WHERE job_id=?",
                (STATUS_RUNNING, time.time(), job_id))
        data = self._payloads.pop(job_id, b"")
        request_id = self.get(job_id)["request_id"]
        if self._processor is None:
            self._mark_failed(job_id, "no processor configured")
            return
        try:
            report = self._processor(data, request_id)
        except Exception as exc:
            self._mark_failed(job_id, f"{type(exc).__name__}: {exc}")
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status=?, finished_at=?, report=?, error=NULL "
                "WHERE job_id=?",
                (STATUS_DONE, time.time(), json.dumps(report), job_id))

    def _mark_failed(self, job_id: str, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET status=?, finished_at=?, error=? "
                "WHERE job_id=?",
                (STATUS_FAILED, time.time(), error, job_id))

    # ------------------------------------------------------------------
    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("report"):
            d["report"] = json.loads(d["report"])
        return d

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, request_id, status, submitted_at, started_at, "
                "finished_at, input_name, input_bytes, error FROM jobs "
                "ORDER BY submitted_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def wait_for(self, job_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(job_id)
            if job is None:
                return None
            if job["status"] in (STATUS_DONE, STATUS_FAILED):
                return job
            time.sleep(0.02)
        return self.get(job_id)

    def shutdown(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
        self._conn.close()

    @property
    def db_path(self) -> str:
        return str(Path(self.settings.db_path))
