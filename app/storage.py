"""SQLite-backed versioned dictionary storage.

Each import creates an immutable *version* (a set of entries). Exactly one
version is active at a time; queries may pin a specific version id. Entries
store their normalized text, length and per-character frequency vector (JSON)
so that the pruning module never needs a second table or a table scan in
Python for cheap filters.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    description  TEXT NOT NULL,
    entry_count  INTEGER NOT NULL,
    active       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries (
    version_id   INTEGER NOT NULL REFERENCES versions(version_id),
    term         TEXT NOT NULL,
    length       INTEGER NOT NULL,
    freq_counts  TEXT NOT NULL,
    frequency    REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_entries_version_length
    ON entries(version_id, length, term);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_only_one_active
    ON versions(active) WHERE active = 1;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VersionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    # Version management
    # ------------------------------------------------------------------ #
    def create_version(
        self,
        entries: list[tuple[str, float]],
        description: str = "",
        activate: bool = True,
    ) -> int:
        """Insert ``(term, usage_frequency)`` pairs as one immutable version.

        Duplicate terms within the import are merged (max frequency wins).
        Returns the new version id.
        """
        merged: dict[str, float] = {}
        for term, freq in entries:
            term = term.strip()
            if not term:
                continue
            merged[term] = max(merged.get(term, 0.0), float(freq))

        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO versions(created_at, description, entry_count, active) "
                "VALUES (?, ?, ?, 0)",
                (_now(), description, len(merged)),
            )
            version_id = int(cur.lastrowid)
            rows = [
                (version_id, term, len(term), json.dumps(Counter(term)), freq)
                for term, freq in sorted(merged.items())
            ]
            conn.executemany(
                "INSERT INTO entries(version_id, term, length, freq_counts, frequency)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            if activate:
                self._set_active(conn, version_id)
        return version_id

    def _set_active(self, conn: sqlite3.Connection, version_id: int) -> None:
        conn.execute("UPDATE versions SET active=0")
        conn.execute("UPDATE versions SET active=1 WHERE version_id=?", (version_id,))

    def set_active(self, version_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if row is None:
                raise KeyError(version_id)
            self._set_active(conn, version_id)

    def active_version(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT version_id FROM versions WHERE active=1").fetchone()
            return int(row["version_id"]) if row else None

    def list_versions(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT version_id, created_at, description, entry_count, active "
                "FROM versions ORDER BY version_id"
            ).fetchall()
            return [dict(r) for r in rows]

    def resolve_version(self, version_id: int | None) -> int:
        active = self.active_version()
        if version_id is not None:
            if not self.version_exists(version_id):
                raise KeyError(version_id)
            return version_id
        if active is None:
            raise KeyError("no active dictionary version")
        return active

    def version_exists(self, version_id: int) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM versions WHERE version_id=?", (version_id,)
            ).fetchone() is not None

    def version_meta(self, version_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version_id, created_at, description, entry_count, active "
                "FROM versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Candidate retrieval
    # ------------------------------------------------------------------ #
    def iter_candidates(
        self,
        version_id: int,
        min_len: int,
        max_len: int,
        limit: int | None = None,
    ) -> Iterator[dict]:
        """Yield entries within a length window in DETERMINISTIC order.

        Ordering is (length, term): length bands scan cheaply via the index
        and ties never depend on SQLite rowid allocation order.
        """
        sql = (
            "SELECT term, length, freq_counts, frequency FROM entries "
            "WHERE version_id=? AND length BETWEEN ? AND ? "
            "ORDER BY length, term"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            for row in conn.execute(sql, (version_id, min_len, max_len)):
                d = dict(row)
                d["freq_counts"] = json.loads(d["freq_counts"])
                yield d

    def count_in_window(self, version_id: int, min_len: int, max_len: int) -> int:
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM entries "
                    "WHERE version_id=? AND length BETWEEN ? AND ?",
                    (version_id, min_len, max_len),
                ).fetchone()["c"]
            )
