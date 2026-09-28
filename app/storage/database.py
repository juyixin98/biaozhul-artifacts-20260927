"""SQLite connection/schema management.

SQLite is used in WAL mode with foreign keys enforced.  All persisted blobs are
exact UTF-8 byte buffers (never re-encoded), and plans are stored as canonical
JSON.  Schema versioning is explicit: the ``meta`` row ``schema_version`` is
checked on open; migrations would branch on it (v1 is the initial schema).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id                  TEXT PRIMARY KEY,         -- caller-provided logical id
    version             INTEGER NOT NULL,          -- 1-based, bumps on replace
    current_sha256      TEXT NOT NULL,
    current_length      INTEGER NOT NULL,
    normalize_newlines  INTEGER NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(id, version)
);

-- One row per immutable source *version*.  Sources are content addressed by
-- sha256; storing the same bytes twice for the same id is a no-op reuse.
CREATE TABLE IF NOT EXISTS source_versions (
    sha256              TEXT PRIMARY KEY,
    data                BLOB NOT NULL,
    length              INTEGER NOT NULL,
    codepoints          INTEGER NOT NULL,
    normalize_newlines  INTEGER NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS rulesets (
    id          TEXT PRIMARY KEY,
    spec_json   TEXT NOT NULL,                    -- canonical RuleSpec[] JSON
    rule_count  INTEGER NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    source_sha256   TEXT NOT NULL,
    source_length   INTEGER NOT NULL,
    ruleset_id      TEXT NOT NULL,
    plan_json       TEXT NOT NULL,                -- Plan.to_json()
    edit_count      INTEGER NOT NULL,
    candidates_total   INTEGER NOT NULL,
    candidates_dropped INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (ruleset_id) REFERENCES rulesets(id)
);

CREATE TABLE IF NOT EXISTS plan_decisions (
    plan_id     TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    stage       TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    start       INTEGER NOT NULL,
    end         INTEGER NOT NULL,
    zero_width  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    conflicts_with TEXT,
    PRIMARY KEY (plan_id, ordinal),
    FOREIGN KEY (plan_id) REFERENCES plans(id)
);

CREATE TABLE IF NOT EXISTS applications (
    id                TEXT PRIMARY KEY,
    plan_id           TEXT NOT NULL UNIQUE,       -- a plan applies at most once
    source_id         TEXT NOT NULL,
    expected_sha256   TEXT NOT NULL,
    result_sha256     TEXT NOT NULL,
    result_length     INTEGER NOT NULL,
    new_source_id     TEXT,
    chunks_emitted    INTEGER NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (plan_id) REFERENCES plans(id)
);

CREATE INDEX IF NOT EXISTS idx_sources_sha ON sources(current_sha256);
CREATE INDEX IF NOT EXISTS idx_plans_source ON plans(source_sha256);
"""


class Database:
    """Owns a sqlite connection and its schema lifecycle."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(
            self.path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,
            # FastAPI dispatches handlers to worker threads; one connection is
            # safe because every write is serialized by an explicit BEGIN/..
            # transaction and the service is single-process for local use.
            check_same_thread=False,
        )
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        # executescript issues its own COMMIT, so it must not run inside our
        # manual BEGIN/COMMIT wrapper.
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"unexpected schema_version {row['value']} (want {SCHEMA_VERSION})"
            )

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        # Serialize writers across FastAPI worker threads.
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                yield cur
            except Exception:
                cur.execute("ROLLBACK")
                raise
            else:
                cur.execute("COMMIT")
            finally:
                cur.close()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def connect(path: str | Path = ":memory:") -> Database:
    return Database(path)
