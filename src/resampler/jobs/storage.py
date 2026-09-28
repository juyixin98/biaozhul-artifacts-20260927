"""SQLite-backed job/state persistence.

Schema
------
jobs     : immutable creation parameters plus lifecycle state and counters
chunks   : every accepted input chunk (for replay/audit)
outputs  : float64 little-endian BLOB per chunk-emitted output segment

The repository deliberately stores *float64* canonical outputs; the
chosen integer/float output encoding is applied when results are read.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class JobRow:
    job_id: str
    state: str
    input_rate: int
    output_rate: int
    up: int
    down: int
    input_format: str
    input_container: str
    output_format: str
    output_container: str
    clip_policy: str
    atten_db: float
    passband_edge: float
    taps_per_phase: int
    num_taps: int
    delay_input: float
    delay_output: float
    passband_edge_hz: float
    stopband_edge_hz: float
    cutoff_hz: float
    input_samples: int
    output_samples: int
    clipped_samples: int
    chunks_received: int
    error_category: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: float
    updated_at: float


class JobStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            import os
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    input_rate INTEGER NOT NULL,
                    output_rate INTEGER NOT NULL,
                    up INTEGER NOT NULL,
                    down INTEGER NOT NULL,
                    input_format TEXT NOT NULL,
                    input_container TEXT NOT NULL,
                    output_format TEXT NOT NULL,
                    output_container TEXT NOT NULL,
                    clip_policy TEXT NOT NULL,
                    atten_db REAL NOT NULL,
                    passband_edge REAL NOT NULL,
                    taps_per_phase INTEGER NOT NULL,
                    num_taps INTEGER NOT NULL,
                    delay_input REAL NOT NULL,
                    delay_output REAL NOT NULL,
                    passband_edge_hz REAL NOT NULL,
                    stopband_edge_hz REAL NOT NULL,
                    cutoff_hz REAL NOT NULL,
                    input_samples INTEGER NOT NULL DEFAULT 0,
                    output_samples INTEGER NOT NULL DEFAULT 0,
                    clipped_samples INTEGER NOT NULL DEFAULT 0,
                    chunks_received INTEGER NOT NULL DEFAULT 0,
                    error_category TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    job_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    input_bytes BLOB NOT NULL,
                    n_samples INTEGER NOT NULL,
                    container TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    PRIMARY KEY (job_id, chunk_index)
                );
                CREATE TABLE IF NOT EXISTS outputs (
                    job_id TEXT NOT NULL,
                    output_index INTEGER NOT NULL,
                    n_samples INTEGER NOT NULL,
                    pcm BLOB NOT NULL,
                    clipped INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (job_id, output_index)
                );
                """
            )

    def create_job(self, row: JobRow) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO jobs (job_id, state, input_rate, output_rate, up, down,
                   input_format, input_container, output_format, output_container,
                   clip_policy, atten_db, passband_edge, taps_per_phase, num_taps,
                   delay_input, delay_output, passband_edge_hz, stopband_edge_hz,
                   cutoff_hz, input_samples, output_samples, clipped_samples,
                   chunks_received, error_category, error_code, error_message,
                   created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row.job_id, row.state, row.input_rate, row.output_rate, row.up, row.down,
                 row.input_format, row.input_container, row.output_format,
                 row.output_container, row.clip_policy, row.atten_db, row.passband_edge,
                 row.taps_per_phase, row.num_taps, row.delay_input, row.delay_output,
                 row.passband_edge_hz, row.stopband_edge_hz, row.cutoff_hz,
                 row.input_samples, row.output_samples, row.clipped_samples,
                 row.chunks_received, row.error_category, row.error_code,
                 row.error_message, row.created_at, row.updated_at))

    @staticmethod
    def _row_factory(cur, row):
        return JobRow(*row)

    def get_job(self, job_id: str) -> JobRow | None:
        self._conn.row_factory = self._row_factory
        with self._lock:
            cur = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
            row = cur.fetchone()
        self._conn.row_factory = None
        return row

    def count_jobs(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def add_chunk(self, job_id: str, index: int, data: bytes, n_samples: int,
                  container: str, ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO chunks (job_id, chunk_index, input_bytes, n_samples, "
                "container, received_at) VALUES (?,?,?,?,?,?)",
                (job_id, index, data, n_samples, container, ts))

    def add_output(self, job_id: str, index: int, arr_f64, clipped: int) -> None:
        blob = arr_f64.astype("<f8", copy=False).tobytes()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO outputs (job_id, output_index, n_samples, pcm, clipped) "
                "VALUES (?,?,?,?,?)", (job_id, index, arr_f64.size, blob, clipped))

    def update_job_counters(self, job_id: str, inputs: int, outputs: int,
                            clipped: int, chunks: int, ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE jobs SET input_samples=?, output_samples=?, clipped_samples=?, "
                "chunks_received=?, updated_at=? WHERE job_id=?",
                (inputs, outputs, clipped, chunks, ts, job_id))

    def set_state(self, job_id: str, state: str, ts: float,
                  error: tuple[str, str, str] | None = None) -> None:
        with self._lock, self._conn:
            if error is None:
                self._conn.execute(
                    "UPDATE jobs SET state=?, updated_at=?, error_category=NULL, "
                    "error_code=NULL, error_message=NULL WHERE job_id=?",
                    (state, ts, job_id))
            else:
                cat, code, msg = error
                self._conn.execute(
                    "UPDATE jobs SET state=?, updated_at=?, error_category=?, "
                    "error_code=?, error_message=? WHERE job_id=?",
                    (state, ts, cat, code, msg, job_id))

    def get_outputs(self, job_id: str) -> list[tuple[int, bytes, int]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT output_index, pcm, clipped FROM outputs "
                "WHERE job_id=? ORDER BY output_index", (job_id,))
            return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
