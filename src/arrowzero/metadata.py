"""Metadata transaction layer backed by SQLite.

Invariant: every service operation leaves exactly one ``operations`` row.
Business writes (array registration, run headers) and the operation audit row
commit together; rejected requests commit only the audit row; unexpected
failures first roll business work back and then insert a ``failed`` row in a
separate transaction, so partial state and missing audit rows are impossible.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Terminal statuses; "unknown" is intentionally not a status — callers must
# pick committed / rejected / failed.
STATUS_COMMITTED = "committed"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    purpose     TEXT NOT NULL,
    versions    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arrays (
    handle          TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    type            TEXT NOT NULL,
    length          INTEGER NOT NULL,
    logical_offset  INTEGER NOT NULL DEFAULT 0,
    null_count      INTEGER NOT NULL DEFAULT 0,
    origin          TEXT NOT NULL,
    import_format   TEXT NOT NULL,
    fingerprints    TEXT NOT NULL,
    registered_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS operations (
    op_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('committed','rejected','failed')),
    error_code   TEXT,
    detail       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_operations_run ON operations(run_id);
CREATE INDEX IF NOT EXISTS idx_arrays_run ON arrays(run_id);
"""


class MetadataStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.cursor()
            yield cur
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ----- runs -----------------------------------------------------------

    def ensure_run(self, run_id: str, purpose: str, versions: dict[str, str]) -> None:
        with self.transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO runs(run_id, purpose, versions) VALUES (?, ?, ?)",
                (run_id, purpose, json.dumps(versions, sort_keys=True)),
            )

    # ----- arrays ---------------------------------------------------------

    def register_array(
        self,
        cur: sqlite3.Cursor,
        *,
        handle: str,
        run_id: str,
        type_name: str,
        length: int,
        logical_offset: int,
        null_count: int,
        origin: str,
        import_format: str,
        fingerprints: list[dict],
    ) -> None:
        cur.execute(
            """INSERT INTO arrays(handle, run_id, type, length, logical_offset, null_count,
                                  origin, import_format, fingerprints)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                handle,
                run_id,
                type_name,
                length,
                logical_offset,
                null_count,
                origin,
                import_format,
                json.dumps(fingerprints, sort_keys=True),
            ),
        )

    def get_array(self, handle: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM arrays WHERE handle = ?", (handle,)
        ).fetchone()
        return dict(row) if row else None

    def list_arrays(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            rows = self._conn.execute("SELECT * FROM arrays ORDER BY registered_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM arrays WHERE run_id = ? ORDER BY registered_at", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- operations -----------------------------------------------------

    def record_operation(
        self,
        *,
        run_id: str | None,
        name: str,
        status: str,
        detail: dict[str, Any],
        error_code: str | None = None,
    ) -> int:
        if status not in (STATUS_COMMITTED, STATUS_REJECTED, STATUS_FAILED):
            raise ValueError(f"unknown operation status {status!r}")
        with self.transaction() as cur:
            cur.execute(
                """INSERT INTO operations(run_id, name, status, error_code, detail)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    run_id,
                    name,
                    status,
                    error_code,
                    json.dumps(detail, default=str, sort_keys=True),
                ),
            )
            return int(cur.lastrowid)

    def list_operations(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if run_id is None:
            rows = self._conn.execute(
                "SELECT * FROM operations ORDER BY op_id ASC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM operations WHERE run_id = ? ORDER BY op_id ASC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            d["versions"] = None
            out.append(d)
        return out

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None
