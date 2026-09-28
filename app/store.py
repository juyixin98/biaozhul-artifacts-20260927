"""SQLite 作业状态、事件环形日志与原始块持久化。

线程模型: FastAPI 同步路由跑在线程池上，这里用单连接 + threading.Lock
串行化所有写入（作业量级小，足够）。

事件环形截断: 每个作业仅保留最近 event_ring 条，保证重放日志有界。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .errors import JobNotFoundError, ResourceExhaustedError, StateConflictError

STATUS_OPEN = "open"
STATUS_FINALIZED = "finalized"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    media_json      TEXT NOT NULL,
    sample_rate     INTEGER,
    channels        INTEGER,
    total_frames    INTEGER NOT NULL DEFAULT 0,
    num_chunks      INTEGER NOT NULL DEFAULT 0,
    committed_json  TEXT NOT NULL DEFAULT '[]',
    intervals_json  TEXT,
    all_final       INTEGER NOT NULL DEFAULT 0,
    error_json      TEXT,
    created_at      REAL NOT NULL,
    finalized_at    REAL
);
CREATE TABLE IF NOT EXISTS chunks (
    job_id      TEXT NOT NULL,
    chunk_seq   INTEGER NOT NULL,
    frames      INTEGER NOT NULL,
    blob        BLOB NOT NULL,
    PRIMARY KEY (job_id, chunk_seq)
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""


class JobStore:
    def __init__(self, db_path: str | Path, event_ring: int = 2000) -> None:
        self.event_ring = event_ring
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ jobs

    def create_job(self, config: dict, media: dict,
                   sample_rate: Optional[int], channels: Optional[int],
                   max_jobs: int) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            if n >= max_jobs:
                raise ResourceExhaustedError(
                    f"job limit reached ({max_jobs})",
                    {"max_jobs": max_jobs})
            self._conn.execute(
                "INSERT INTO jobs(job_id,status,config_json,media_json,"
                "sample_rate,channels,created_at) VALUES(?,?,?,?,?,?,?)",
                (job_id, STATUS_OPEN, json.dumps(config),
                 json.dumps(media), sample_rate, channels, time.time()))
            self._conn.commit()
        return job_id

    def get_job(self, job_id: str) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(f"job {job_id!r} not found",
                                   {"job_id": job_id})
        return row

    def require_state(self, job_id: str, *allowed: str) -> sqlite3.Row:
        row = self.get_job(job_id)
        if row["status"] not in allowed:
            raise StateConflictError(
                f"job {job_id} is {row['status']!r}, "
                f"requires one of {list(allowed)}",
                {"job_id": job_id, "current": row["status"],
                 "required": list(allowed)})
        return row

    # ---------------------------------------------------------------- chunks

    def add_chunk(self, job_id: str, blob: bytes, frames: int) -> int:
        with self._lock:
            row = self.require_state(job_id, STATUS_OPEN)
            seq = row["num_chunks"]
            try:
                self._conn.execute(
                    "INSERT INTO chunks(job_id,chunk_seq,frames,blob) "
                    "VALUES(?,?,?,?)", (job_id, seq, frames,
                                        sqlite3.Binary(blob)))
            except sqlite3.OperationalError as e:
                # 磁盘满 / I/O 错误归类为资源耗尽
                raise ResourceExhaustedError(
                    f"failed to persist chunk: {e}") from e
            self._conn.execute(
                "UPDATE jobs SET num_chunks=num_chunks+1, "
                "total_frames=total_frames+? WHERE job_id=?",
                (frames, job_id))
            self._conn.commit()
            return seq

    def update_committed(self, job_id: str, intervals: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET committed_json=? WHERE job_id=?",
                (json.dumps(intervals), job_id))
            self._conn.commit()

    def mark_finalized(self, job_id: str, intervals: list[dict],
                       total_frames: int, sample_rate: int,
                       channels: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, intervals_json=?, all_final=1, "
                "finalized_at=?, total_frames=?, sample_rate=?, channels=? "
                "WHERE job_id=?",
                (STATUS_FINALIZED, json.dumps(intervals), time.time(),
                 total_frames, sample_rate, channels, job_id))
            self._conn.commit()

    def mark_failed(self, job_id: str, error: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, error_json=? WHERE job_id=?",
                (STATUS_FAILED, json.dumps(error), job_id))
            self._conn.commit()

    def iter_chunks(self, job_id: str):
        with self._lock:
            rows = self._conn.execute(
                "SELECT chunk_seq,frames,blob FROM chunks WHERE job_id=? "
                "ORDER BY chunk_seq", (job_id,)).fetchall()
        for r in rows:
            yield r["chunk_seq"], r["frames"], bytes(r["blob"])

    # ---------------------------------------------------------------- events

    def add_event(self, job_id: str, run_id: str, kind: str,
                  payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(job_id,run_id,kind,payload_json,ts) "
                "VALUES(?,?,?,?,?)",
                (job_id, run_id, kind, json.dumps(payload, default=str),
                 time.time()))
            # 环形截断：仅保留最近 event_ring 条
            self._conn.execute(
                "DELETE FROM events WHERE job_id=? AND id NOT IN "
                "(SELECT id FROM events WHERE job_id=? ORDER BY id DESC "
                " LIMIT ?)", (job_id, job_id, self.event_ring))
            self._conn.commit()

    def list_events(self, job_id: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id,kind,payload_json,ts FROM events "
                "WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit)).fetchall()
        out = []
        for r in reversed(rows):
            p = {"run_id": r["run_id"], "kind": r["kind"], "ts": r["ts"]}
            p.update(json.loads(r["payload_json"]))
            out.append(p)
        return out
