"""Job state: SQLite-backed store plus the synchronous parse runner.

Every job records its input identity (path, size, sha256), a status
(queued -> running -> done | failed), a classified error on failure, and an
ordered event log of the computation steps so a result can be traced back to
what produced it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid

from .boxes import parse_movie
from .errors import InputError, MP4Error
from .timeline import build_movie_timeline, movie_timeline_to_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    input_sha256 TEXT,
    input_size INTEGER,
    error_category TEXT,
    error_message TEXT,
    created REAL NOT NULL,
    finished REAL,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS job_events (
    job_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (job_id, seq)
);
"""


class JobStore:
    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def create(self, path: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, path, status, created) VALUES (?,?,?,?)",
                (job_id, path, "queued", time.time()),
            )
        self.add_event(job_id, "created", f"path={path}")
        return job_id

    def add_event(self, job_id: str, event: str, detail: str | None = None) -> None:
        with self._lock, self._conn:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM job_events WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO job_events (job_id, seq, ts, event, detail) VALUES (?,?,?,?,?)",
                (job_id, seq, time.time(), event, detail),
            )

    def _set(self, job_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def mark_running(self, job_id: str, sha256: str, size: int) -> None:
        self._set(job_id, status="running", input_sha256=sha256, input_size=size)

    def mark_done(self, job_id: str, result: dict) -> None:
        self._set(job_id, status="done", finished=time.time(), result_json=json.dumps(result))

    def mark_failed(self, job_id: str, category: str, message: str) -> None:
        self._set(
            job_id,
            status="failed",
            error_category=category,
            error_message=message,
            finished=time.time(),
        )

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        job.pop("result_json", None)
        return job

    def result(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT result_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return json.loads(row["result_json"])

    def events(self, job_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, event, detail FROM job_events WHERE job_id=? ORDER BY seq",
                (job_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def run_parse_job(store: JobStore, job_id: str, path: str, max_file_bytes: int) -> None:
    """Run the full pipeline for one job, recording every step.  Failures are
    classified and stored — never silently converted into success."""
    try:
        if not os.path.isfile(path):
            raise InputError(f"input file not found: {path}")
        size = os.path.getsize(path)
        if size > max_file_bytes:
            raise InputError(f"input file {size} bytes exceeds limit {max_file_bytes}")
        with open(path, "rb") as fh:
            data = fh.read()
        digest = hashlib.sha256(data).hexdigest()
        store.mark_running(job_id, digest, size)
        store.add_event(job_id, "input_read", f"size={size} sha256={digest[:16]}...")

        movie = parse_movie(data)
        store.add_event(
            job_id,
            "boxes_parsed",
            f"movie_timescale={movie.movie_timescale} tracks={len(movie.tracks)} "
            f"brands={','.join(movie.brands)}",
        )

        timeline = build_movie_timeline(movie)
        for t in timeline.tracks:
            store.add_event(
                job_id,
                "track_timeline_built",
                f"track={t.track_id} handler={t.handler} media_ts={t.media_timescale} "
                f"samples={len(t.samples)} presentations={len(t.presentations)} "
                f"gaps={len(t.gaps)} movie_duration={t.movie_duration}",
            )

        result = movie_timeline_to_dict(timeline)
        result["source"] = {"path": path, "sha256": digest, "size": size}
        store.mark_done(job_id, result)
        store.add_event(job_id, "done", "timeline computed")
    except MP4Error as exc:
        store.mark_failed(job_id, exc.category, str(exc))
        store.add_event(job_id, "failed", f"{exc.category}: {exc}")
    except Exception as exc:  # unexpected: still recorded as failure, never success
        store.mark_failed(job_id, "internal_error", f"{type(exc).__name__}: {exc}")
        store.add_event(job_id, "failed", f"internal_error: {type(exc).__name__}: {exc}")
