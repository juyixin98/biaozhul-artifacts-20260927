r"""Job persistence: SQLite metadata + append-only raw output blobs on disk.

Job lifecycle states::

    created -> running -> flushed -> completed
                           \-> failed (terminal)

Only metadata is kept in SQLite; produced output samples are streamed to
``<data_dir>/<job_id>.out.f64`` (little-endian float64) so a large job never
has to be buffered in memory or copied into a row.  Each push records its
input/output counters, giving a replayable audit trail of chunk boundaries.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from .errors import NotFoundError, StateConflictError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    fin             INTEGER NOT NULL,
    fout            INTEGER NOT NULL,
    l               INTEGER NOT NULL,
    m               INTEGER NOT NULL,
    output_dtype    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    total_in        INTEGER NOT NULL DEFAULT 0,
    total_out       INTEGER NOT NULL DEFAULT 0,
    chunks          INTEGER NOT NULL DEFAULT 0,
    design_info     TEXT NOT NULL,
    error_code      TEXT,
    error_message   TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    job_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    n_in        INTEGER NOT NULL,
    n_out       INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (job_id, seq)
);
"""

TERMINAL_STATES = {"completed", "failed"}


class JobStore:
    def __init__(self, db_path: str, data_dir: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".",
                    exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = db_path
        self.data_dir = data_dir
        self._conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def output_path(self, job_id: str) -> str:
        return os.path.join(self.data_dir, f"{job_id}.out.f64")

    def append_output(self, job_id: str, samples) -> int:
        import numpy as np
        path = self.output_path(job_id)
        b = np.asarray(samples, dtype="<f8").tobytes()
        with open(path, "ab") as fh:
            fh.write(b)
        return len(b) // 8

    # --------------------------------------------------------------- CRUD-ish
    def create_job(self, *, fin: int, fout: int, l: int, m: int,
                   output_dtype: str, design_info: dict) -> dict:
        job_id = uuid.uuid4().hex
        now = time.time()
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO jobs(job_id,state,fin,fout,l,m,output_dtype,"
                "created_at,updated_at,total_in,total_out,chunks,design_info)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, "created", fin, fout, l, m, output_dtype,
                 now, now, 0, 0, 0, json.dumps(design_info, sort_keys=True)),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        cur = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        row = cur.fetchone()
        if row is None:
            raise NotFoundError(
                f"job {job_id} not found", details={"job_id": job_id})
        job = dict(row)
        job["design_info"] = json.loads(job["design_info"])
        return job

    def require_state(self, job_id: str, *allowed: str) -> dict:
        job = self.get_job(job_id)
        if job["state"] not in allowed:
            raise StateConflictError(
                f"job {job_id} is in state '{job['state']}', "
                f"expected one of {list(allowed)}",
                details={"job_id": job_id, "state": job["state"],
                         "allowed": list(allowed)},
            )
        return job

    def mark_running(self, job_id: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE jobs SET state='running', updated_at=? WHERE job_id=?",
                (time.time(), job_id))

    def record_chunk(self, job_id: str, kind: str, n_in: int, n_out: int) -> None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT chunks FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            seq = row[0]
            conn.execute(
                "INSERT INTO chunks(job_id,seq,kind,n_in,n_out,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (job_id, seq, kind, n_in, n_out, time.time()))
            conn.execute(
                "UPDATE jobs SET total_in=total_in+?, total_out=total_out+?, "
                "chunks=chunks+1, state=CASE WHEN state='created' THEN 'running' "
                "ELSE state END, updated_at=? WHERE job_id=?",
                (n_in, n_out, time.time(), job_id))

    def mark_completed(self, job_id: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE jobs SET state='completed', updated_at=? WHERE job_id=?",
                (time.time(), job_id))

    def mark_failed(self, job_id: str, error_code: str, message: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE jobs SET state='failed', error_code=?, error_message=?, "
                "updated_at=? WHERE job_id=?",
                (error_code, message, time.time(), job_id))

    def list_jobs(self, limit: int = 100) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        jobs = []
        for row in cur.fetchall():
            job = dict(row)
            job["design_info"] = json.loads(job["design_info"])
            jobs.append(job)
        return jobs

    def get_chunks(self, job_id: str) -> list[dict]:
        self.get_job(job_id)
        cur = self._conn.execute(
            "SELECT seq,kind,n_in,n_out,created_at FROM chunks "
            "WHERE job_id=? ORDER BY seq", (job_id,))
        return [dict(r) for r in cur.fetchall()]

    def read_output(self, job_id: str):
        import numpy as np
        self.get_job(job_id)
        path = self.output_path(job_id)
        if not os.path.exists(path):
            return np.empty(0, dtype=np.float64)
        raw = np.fromfile(path, dtype="<f8")
        return raw.astype(np.float64)
