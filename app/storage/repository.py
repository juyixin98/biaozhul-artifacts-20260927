"""SQLite-backed versioned dictionary repository.

A publish is one transaction: the new version row and ALL its entry rows
become visible together, and until it commits readers keep seeing the previous
current version. Old versions are retained so a request that pinned a version
keeps reading exactly that version for its whole lifetime.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import PreparedEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version         TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    is_current      INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    entry_count     INTEGER NOT NULL,
    total_frequency INTEGER NOT NULL,
    checksum        TEXT NOT NULL,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    version          TEXT NOT NULL REFERENCES versions(version),
    surface          TEXT NOT NULL,
    norm_key         TEXT NOT NULL,
    frequency        INTEGER NOT NULL,
    explicit_cost    INTEGER NOT NULL,
    cost_value       REAL,
    UNIQUE (version, norm_key)
);
CREATE INDEX IF NOT EXISTS idx_entries_version ON entries(version);
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class StoredEntry:
    surface: str
    key: str
    frequency: int
    explicit_cost: bool
    cost_value: Optional[float]


@dataclass(frozen=True)
class VersionInfo:
    version: str
    created_at: str
    entry_count: int
    total_frequency: int
    checksum: str
    note: Optional[str]
    is_current: bool


class DictionaryRepository:
    """Thread-safe thin wrapper over one SQLite file."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")

    # ------------------------------------------------------------------ read
    def current_version(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM versions WHERE is_current = 1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return row["version"] if row else None

    def resolve_version(self, ref: Optional[str]) -> Optional[str]:
        """Resolve "current"/None to the current version; verify pinned ones."""
        if ref in (None, "", "current"):
            return self.current_version()
        with self._connect() as conn:
            row = conn.execute("SELECT version FROM versions WHERE version = ?", (ref,)).fetchone()
            return row["version"] if row else None

    def list_versions(self) -> list[VersionInfo]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM versions ORDER BY created_at DESC"
            ).fetchall()
            return [
                VersionInfo(
                    version=r["version"],
                    created_at=r["created_at"],
                    entry_count=r["entry_count"],
                    total_frequency=r["total_frequency"],
                    checksum=r["checksum"],
                    note=r["note"],
                    is_current=bool(r["is_current"]),
                )
                for r in rows
            ]

    def load_entries(self, version: str) -> tuple[list[StoredEntry], int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT surface, norm_key, frequency, explicit_cost, cost_value "
                "FROM entries WHERE version = ? ORDER BY id",
                (version,),
            ).fetchall()
            entries = [
                StoredEntry(
                    surface=r["surface"],
                    key=r["norm_key"],
                    frequency=r["frequency"],
                    explicit_cost=bool(r["explicit_cost"]),
                    cost_value=r["cost_value"],
                )
                for r in rows
            ]
            total_row = conn.execute(
                "SELECT total_frequency FROM versions WHERE version = ?", (version,)
            ).fetchone()
            total = total_row["total_frequency"] if total_row else 0
        return entries, total

    def has_any_version(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM versions LIMIT 1").fetchone() is not None

    # ----------------------------------------------------------------- write
    def publish(self, entries: list[PreparedEntry], note: Optional[str] = None) -> str:
        """Atomically publish a complete new version and make it current."""
        checksum = self._checksum(entries)
        version = self._build_version_id(entries, checksum)
        created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        total_frequency = sum(e.frequency for e in entries if e.cost is None)

        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT version FROM versions WHERE version = ?", (version,)
            ).fetchone()
            conn.execute("UPDATE versions SET is_current = 0")
            if existing is None:
                conn.execute(
                    "INSERT INTO versions (version, created_at, is_current, entry_count, "
                    "total_frequency, checksum, note) VALUES (?, ?, 1, ?, ?, ?, ?)",
                    (version, created_at, len(entries), total_frequency, checksum, note),
                )
                conn.executemany(
                    "INSERT INTO entries (version, surface, norm_key, frequency, explicit_cost, "
                    "cost_value) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            version,
                            e.surface,
                            e.key,
                            e.frequency,
                            1 if e.cost is not None else 0,
                            e.cost,
                        )
                        for e in entries
                    ],
                )
            else:
                # Identical content (content-addressed id): republish is
                # idempotent and simply re-selects it as current.
                conn.execute("UPDATE versions SET is_current = 1 WHERE version = ?", (version,))
        return version

    @staticmethod
    def _checksum(entries: list[PreparedEntry]) -> str:
        payload = [
            {"surface": e.surface, "key": e.key, "frequency": e.frequency,
             "cost": e.cost}
            for e in entries
        ]
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def _build_version_id(self, entries: list[PreparedEntry], checksum: str) -> str:
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"v-{date_part}-{checksum[:10]}"
