"""SQLite 快照与作业状态存储。

- snapshots: 每个 stream 的已解析播放列表版本(原始文本 + 解析结果 JSON);
- jobs: 异步风格的作业状态机(PENDING → RUNNING → DONE/FAILED),
  对比与计划请求都落一条作业记录,诊断可回放。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict

from .models import ByteRange, CompareReport, Playlist, Segment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    stream_id   TEXT NOT NULL,
    version_no  INTEGER NOT NULL,
    raw         TEXT NOT NULL,
    playlist    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (stream_id, version_no)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    stream_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    result      TEXT,
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""


def playlist_to_json(pl: Playlist) -> str:
    def seg(s: Segment) -> dict:
        return {
            "sequence": s.sequence,
            "uri": s.uri,
            "duration": s.duration,
            "title": s.title,
            "discontinuity": s.discontinuity,
            "discontinuity_sequence": s.discontinuity_sequence,
            "byte_range": asdict(s.byte_range) if s.byte_range else None,
        }

    return json.dumps(
        {
            "version": pl.version,
            "target_duration": pl.target_duration,
            "media_sequence": pl.media_sequence,
            "discontinuity_sequence": pl.discontinuity_sequence,
            "playlist_type": pl.playlist_type,
            "endlist": pl.endlist,
            "segments": [seg(s) for s in pl.segments],
        }
    )


def playlist_from_json(payload: str) -> Playlist:
    d = json.loads(payload)
    segments = []
    for s in d["segments"]:
        br = s["byte_range"]
        segments.append(
            Segment(
                sequence=s["sequence"],
                uri=s["uri"],
                duration=s["duration"],
                title=s["title"],
                discontinuity=s["discontinuity"],
                discontinuity_sequence=s["discontinuity_sequence"],
                byte_range=ByteRange(**br) if br else None,
            )
        )
    return Playlist(
        version=d["version"],
        target_duration=d["target_duration"],
        media_sequence=d["media_sequence"],
        discontinuity_sequence=d["discontinuity_sequence"],
        playlist_type=d["playlist_type"],
        endlist=d["endlist"],
        segments=tuple(segments),
    )


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ---- 快照 ----

    def add_snapshot(self, stream_id: str, raw: str, playlist: Playlist) -> int:
        with self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS v FROM snapshots WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
            version_no = int(row["v"])
            self._conn.execute(
                "INSERT INTO snapshots (stream_id, version_no, raw, playlist, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (stream_id, version_no, raw, playlist_to_json(playlist), time.time()),
            )
        return version_no

    def get_snapshot(self, stream_id: str, version_no: int | None = None) -> Playlist | None:
        if version_no is None:
            row = self._conn.execute(
                "SELECT playlist FROM snapshots WHERE stream_id = ?"
                " ORDER BY version_no DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT playlist FROM snapshots WHERE stream_id = ? AND version_no = ?",
                (stream_id, version_no),
            ).fetchone()
        return playlist_from_json(row["playlist"]) if row else None

    def list_versions(self, stream_id: str) -> list[int]:
        rows = self._conn.execute(
            "SELECT version_no FROM snapshots WHERE stream_id = ? ORDER BY version_no",
            (stream_id,),
        ).fetchall()
        return [int(r["version_no"]) for r in rows]

    # ---- 作业 ----

    def create_job(self, stream_id: str, kind: str, request_id: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO jobs (job_id, stream_id, kind, state, request_id, created_at, updated_at)"
                " VALUES (?, ?, ?, 'PENDING', ?, ?, ?)",
                (job_id, stream_id, kind, request_id, now, now),
            )
        return job_id

    def finish_job(
        self,
        job_id: str,
        state: str,
        result: CompareReport | dict | None = None,
        error: str | None = None,
    ) -> None:
        payload = json.dumps(result, default=lambda o: asdict(o)) if result is not None else None
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET state = ?, result = ?, error = ?, updated_at = ? WHERE job_id = ?",
                (state, payload, error, time.time(), job_id),
            )

    def get_job(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        if out["result"]:
            out["result"] = json.loads(out["result"])
        return out
