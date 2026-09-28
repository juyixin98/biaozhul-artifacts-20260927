"""SQLite metadata store with explicit transaction boundaries.

A run is persisted atomically: either the run row, every batch row and all
progress events commit together, or NOTHING is committed (the failure path
uses a separate transaction so a failed run is recorded, but no partial
success state can ever be read back).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..core.errors import RunConflict, RunNotFound

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('ok','failed')),
    sort_policy         TEXT,
    width_policy        TEXT,
    target_width        INTEGER,
    global_index_width  INTEGER,
    cardinality         INTEGER,
    batch_count         INTEGER,
    error_category      TEXT,
    error_message       TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    batch_id        TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    row_count       INTEGER,
    null_rows       INTEGER,
    declared_entries INTEGER,
    distinct_values INTEGER,
    used_entries    INTEGER,
    unused_declared INTEGER,
    duplicate_declared INTEGER,
    local_to_global TEXT,
    global_indices TEXT,
    valid TEXT,
    UNIQUE(run_id, batch_id)
);
CREATE TABLE IF NOT EXISTS global_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    global_code INTEGER NOT NULL,
    type        TEXT NOT NULL,
    value       TEXT NOT NULL,
    UNIQUE(run_id, global_code)
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    ordinal     INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MetadataStore:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def exists(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row is not None

    def save_success(self, run_id: str, encoding,
                     events: list[dict]) -> None:
        """Commit run + batches + events in ONE atomic transaction."""
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO runs (run_id, created_at, status, "
                    "sort_policy, width_policy, target_width, "
                    "global_index_width, cardinality, batch_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, _now(), "ok", encoding.sort_policy,
                     encoding.width_policy, encoding.target_width,
                     encoding.global_index_width, encoding.cardinality,
                     len(encoding.batches)))
                for ordinal, rb in enumerate(encoding.batches):
                    s = rb.stats
                    conn.execute(
                        "INSERT INTO batches (run_id, batch_id, ordinal, "
                        "row_count, null_rows, declared_entries, "
                        "distinct_values, used_entries, unused_declared, "
                        "duplicate_declared, local_to_global, "
                        "global_indices, valid) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, rb.batch_id, ordinal, s.row_count,
                         s.null_rows, s.declared_entries, s.distinct_values,
                         s.used_entries, s.unused_declared,
                         s.duplicate_declared,
                         json.dumps(list(rb.local_to_global)),
                         json.dumps(list(rb.global_indices)),
                         json.dumps([bool(v) for v in rb.valid])))
                for code, (t, v) in enumerate(zip(encoding.global_types,
                                                  encoding.global_values)):
                    conn.execute(
                        "INSERT INTO global_entries (run_id, global_code, "
                        "type, value) VALUES (?,?,?,?)",
                        (run_id, code, t, str(v)))
                self._insert_events(conn, run_id, events)
            except sqlite3.IntegrityError as exc:
                # Concurrent/duplicate run_id: roll back the whole attempt.
                conn.rollback()
                raise RunConflict(
                    f"run_id {run_id!r} already exists") from exc

    def save_failure(self, run_id: str, category: str, message: str,
                     events: list[dict]) -> None:
        """Persist a failed run in its own transaction.

        Called only after the success transaction has been rolled back, so
        this can never leave half-written batch rows behind.
        """
        with self._connect() as conn:
            # INSERT OR IGNORE: a failure record must never overwrite an
            # already-committed run (e.g. a run-id conflict against a
            # successful run leaves the successful run intact).
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, created_at, status, "
                "error_category, error_message) VALUES (?,?,?,?,?)",
                (run_id, _now(), "failed", category, message))
            affected = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (run_id,)).fetchone()
            if affected is not None and affected["status"] == "failed":
                conn.execute("DELETE FROM batches WHERE run_id = ?",
                             (run_id,))
                conn.execute("DELETE FROM global_entries WHERE run_id = ?",
                             (run_id,))
                conn.execute("DELETE FROM events WHERE run_id = ?",
                             (run_id,))
                self._insert_events(conn, run_id, events)

    def _insert_events(self, conn, run_id: str, events: list[dict]) -> None:
        for ordinal, payload in enumerate(events):
            conn.execute(
                "INSERT INTO events (run_id, ordinal, recorded_at, payload) "
                "VALUES (?,?,?,?)",
                (run_id, ordinal, _now(), json.dumps(payload, default=str)))

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            run = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise RunNotFound(f"unknown run_id {run_id!r}")
            batches = [dict(r) for r in conn.execute(
                "SELECT batch_id, ordinal, row_count, null_rows, "
                "declared_entries, distinct_values, used_entries, "
                "unused_declared, duplicate_declared, local_to_global, "
                "global_indices, valid "
                "FROM batches WHERE run_id = ? ORDER BY ordinal",
                (run_id,)).fetchall()]
            entries = [
                {"global_code": r["global_code"], "type": r["type"],
                 "value": int(r["value"]) if r["type"] == "int64"
                         else r["value"]}
                for r in conn.execute(
                    "SELECT global_code, type, value FROM global_entries "
                    "WHERE run_id = ? ORDER BY global_code",
                    (run_id,)).fetchall()]
            events = [json.loads(r["payload"]) for r in conn.execute(
                "SELECT payload FROM events WHERE run_id = ? "
                "ORDER BY ordinal", (run_id,)).fetchall()]
        out = dict(run)
        for b in batches:
            b["local_to_global"] = json.loads(b["local_to_global"])
            b["global_indices"] = json.loads(b["global_indices"])
            b["valid"] = json.loads(b["valid"])
        out["batches"] = batches
        out["global_entries"] = entries
        out["events"] = events
        return out
