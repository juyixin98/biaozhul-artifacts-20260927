"""SQLite-backed metadata store with explicit job-state transactions.

State machine::

    RUNNING --commit--> SUCCEEDED
    RUNNING --fail----> FAILED
    RUNNING (process killed) -> row stays RUNNING (no silent success)

Every transition happens in a single ``BEGIN IMMEDIATE`` transaction and the
final status can only move forward via the guarded ``..._status`` columns, so
an exception path can never leave a job reported as successful.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id              TEXT PRIMARY KEY,
    status              TEXT NOT NULL CHECK (status IN ('RUNNING','SUCCEEDED','FAILED')),
    value_type          TEXT NOT NULL,
    index_policy        TEXT NOT NULL,
    target_width        INTEGER,
    sort_policy         TEXT NOT NULL,
    index_width_bits    INTEGER,
    cardinality         INTEGER,
    stats_json          TEXT,
    normalization_json  TEXT,
    failure_code        TEXT,
    failure_message     TEXT,
    failure_details_json TEXT,
    versions_json       TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at         TEXT
);

CREATE TABLE IF NOT EXISTS job_batches (
    job_id      TEXT NOT NULL REFERENCES jobs(job_id),
    ordinal     INTEGER NOT NULL,
    batch_id    TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    null_count  INTEGER NOT NULL,
    PRIMARY KEY (job_id, ordinal)
);
"""

_TERMINAL = {"SUCCEEDED", "FAILED"}


class JobStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        # NB: sqlite3's executescript() issues a COMMIT before running, which
        # would break the explicit transaction below — execute DDL one by one.
        statements = [s.strip() for s in _SCHEMA.split(";") if s.strip()]
        with self._tx() as cur:
            for stmt in statements:
                cur.execute(stmt)
            cur.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        # BEGIN IMMEDIATE serializes writers; commit/rollback bracket the unit.
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn.cursor()
                self._conn.execute("COMMIT")
            except Exception:
                # Some statements (e.g. executescript) may end the txn; guard
                # the rollback so the original exception is not masked.
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ writes

    def insert_running(
        self,
        *,
        job_id: str,
        value_type: str,
        index_policy: str,
        target_width: int | None,
        sort_policy: str,
        versions: dict[str, str],
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO jobs(job_id, status, value_type, index_policy,
                                    target_width, sort_policy, versions_json)
                   VALUES (?, 'RUNNING', ?, ?, ?, ?, ?)""",
                (job_id, value_type, index_policy, target_width, sort_policy,
                 json.dumps(versions, sort_keys=True)),
            )

    def mark_succeeded(self, *, job_id: str, result, normalization: list[dict]) -> None:
        stats = dict(result.stats)
        with self._tx() as cur:
            row = cur.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            self._guard_open(row, job_id)
            cur.execute(
                """UPDATE jobs SET status='SUCCEEDED',
                       index_width_bits=?, cardinality=?, stats_json=?,
                       normalization_json=?,
                       finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE job_id=?""",
                (result.index_width_bits, result.cardinality,
                 json.dumps(stats, sort_keys=True),
                 json.dumps(normalization, sort_keys=True), job_id),
            )
            cur.executemany(
                """INSERT INTO job_batches(job_id, ordinal, batch_id,
                                           row_count, null_count)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (job_id, i, r.batch_id, r.row_count, r.null_count)
                    for i, r in enumerate(result.batch_remaps)
                ],
            )

    def mark_failed(self, *, job_id: str, code: str, message: str,
                    details: dict[str, Any]) -> None:
        with self._tx() as cur:
            row = cur.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            self._guard_open(row, job_id)
            cur.execute(
                """UPDATE jobs SET status='FAILED', failure_code=?,
                       failure_message=?, failure_details_json=?,
                       finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE job_id=?""",
                (code, message, json.dumps(details, sort_keys=True, default=repr),
                 job_id),
            )

    @staticmethod
    def _guard_open(row: sqlite3.Row | None, job_id: str) -> None:
        if row is None:
            raise RuntimeError(f"unknown job_id {job_id!r}")
        if row["status"] in _TERMINAL:
            raise RuntimeError(
                f"job {job_id!r} already terminal ({row['status']}); "
                "status transitions are one-shot"
            )

    # ------------------------------------------------------------------- reads

    def get_job(self, job_id: str) -> dict | None:
        with self._tx() as cur:
            row = cur.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                return None
            batches = cur.execute(
                "SELECT ordinal, batch_id, row_count, null_count "
                "FROM job_batches WHERE job_id=? ORDER BY ordinal",
                (job_id,),
            ).fetchall()
        d = dict(row)
        for json_col in ("stats_json", "normalization_json",
                         "failure_details_json", "versions_json"):
            d[json_col] = json.loads(d[json_col]) if d[json_col] is not None else None
        d["batches"] = [dict(b) for b in batches]
        return d
