"""SQLite connection management and schema definition."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    encoding      TEXT NOT NULL,
    case_mode     TEXT NOT NULL,
    pattern_count INTEGER NOT NULL,
    node_count    INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patterns (
    version_id TEXT NOT NULL REFERENCES versions(version_id),
    pattern_id INTEGER NOT NULL,
    pattern_b64 TEXT NOT NULL,
    length     INTEGER NOT NULL,
    PRIMARY KEY (version_id, pattern_id)
);

CREATE TABLE IF NOT EXISTS scans (
    scan_id        TEXT PRIMARY KEY,
    version_id     TEXT NOT NULL REFERENCES versions(version_id),
    state          TEXT NOT NULL CHECK (state IN ('open', 'closed')),
    current_node   INTEGER NOT NULL,
    bytes_consumed INTEGER NOT NULL,
    epoch          INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id   TEXT NOT NULL REFERENCES scans(scan_id),
    epoch     INTEGER NOT NULL,
    seq       INTEGER NOT NULL,
    start_off INTEGER NOT NULL,
    end_off   INTEGER NOT NULL,
    pat_id    INTEGER NOT NULL,
    UNIQUE (scan_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_hits_scan_seq ON hits (scan_id, seq);

CREATE TABLE IF NOT EXISTS diagnostic_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    kind       TEXT NOT NULL CHECK (kind IN ('request', 'decision')),
    method     TEXT,
    path       TEXT,
    decision   TEXT,
    code       TEXT,
    summary    TEXT NOT NULL,
    state_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diag_request ON diagnostic_events (request_id);
CREATE INDEX IF NOT EXISTS idx_diag_created ON diagnostic_events (created_at, id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        # check_same_thread=False: FastAPI sync handlers run in a threadpool;
        # all writes go through self._lock.
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers; readers may use :meth:`execute` directly."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
