"""SQLite state and audit query interface.

Two tables:

* ``runs`` — one row per inspection request (accepted or rejected)
* ``events`` — one row per audited processing step (progress + decision basis)

The database lives entirely under the service home.  Connections use
``check_same_thread=False`` guarded by a lock; the FastAPI app is single
process and this keeps usage simple.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    status        TEXT NOT NULL CHECK (status IN ('accepted','rejected','error')),
    input_name    TEXT NOT NULL,
    input_sha256  TEXT NOT NULL,
    input_size    INTEGER NOT NULL,
    format        TEXT,
    category      TEXT,
    detail        TEXT,
    entry         TEXT,
    file_count    INTEGER,
    total_bytes   INTEGER,
    manifest_path TEXT,
    created_ts    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS events (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    phase         TEXT NOT NULL,
    name          TEXT NOT NULL,
    detail_json   TEXT NOT NULL,
    record_hash   TEXT NOT NULL,
    ts            TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
"""


class Store:
    def __init__(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        self._path = home / "runs.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #

    def create_run(
        self,
        run_id: str,
        *,
        input_name: str,
        input_sha256: str,
        input_size: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, status, input_name, input_sha256, "
                "input_size) VALUES (?, 'error', ?, ?, ?)",
                (run_id, input_name, input_sha256, input_size),
            )
            self._conn.commit()

    def record_event(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (run_id, phase, name, detail_json, "
                "record_hash, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record["run_id"],
                    record["phase"],
                    record["event"],
                    json.dumps(record["detail"], sort_keys=True, ensure_ascii=False),
                    record["record_hash"],
                    record["ts"],
                ),
            )
            self._conn.commit()

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        fmt: str | None,
        category: str | None,
        detail: str | None,
        entry: str | None,
        file_count: int | None,
        total_bytes: int | None,
        manifest_path: str | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status=?, format=?, category=?, detail=?, "
                "entry=?, file_count=?, total_bytes=?, manifest_path=? "
                "WHERE run_id=?",
                (
                    status,
                    fmt,
                    category,
                    detail,
                    entry,
                    file_count,
                    total_bytes,
                    manifest_path,
                    run_id,
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, status, input_name, format, category, "
                "file_count, total_bytes, created_ts FROM runs "
                "ORDER BY created_ts DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, run_id, phase, name, detail_json, record_hash, ts "
                "FROM events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM runs GROUP BY status"
            ).fetchall()
            total = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        counts = {r["status"]: r["n"] for r in rows}
        return {
            "accepted": counts.get("accepted", 0),
            "rejected": counts.get("rejected", 0),
            "error": counts.get("error", 0),
            "events": int(total),
        }
