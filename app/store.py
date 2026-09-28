"""SQLite-backed immutable version store for the lexicon.

Versions are published atomically and whole: a new ``versions`` row and all
its ``entries`` rows are written in one transaction; readers never observe a
half-published dictionary. Each published version is frozen — it can be
read but never mutated in place — so a request that pinned version N keeps
using exactly that data while N+1 is being served to new requests.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .lexicon import LexiconVersion, WordEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id   INTEGER PRIMARY KEY,
    published_at TEXT    NOT NULL,
    word_count   INTEGER NOT NULL,
    total_freq   INTEGER NOT NULL,
    checksum     TEXT    NOT NULL,
    note         TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS entries (
    version_id INTEGER NOT NULL REFERENCES versions(version_id),
    word       TEXT    NOT NULL,
    freq       INTEGER NOT NULL,
    PRIMARY KEY (version_id, word)
);
"""


class VersionNotFoundError(LookupError):
    """Raised when a client pins a version_id that does not exist."""


class VersionStore:
    """Owns the SQLite connection and an LRU-ish cache of built versions."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + an explicit write lock: reads are
        # serialized by SQLite; publish() additionally serializes writers.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._write_lock = threading.Lock()
        self._cache: dict[int, LexiconVersion] = {}

    def close(self) -> None:
        self._conn.close()

    # ---- reads ---------------------------------------------------------

    def latest_version_id(self) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(version_id) AS m FROM versions"
        ).fetchone()
        return None if row["m"] is None else int(row["m"])

    def list_versions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT version_id, published_at, word_count, total_freq, checksum, note "
            "FROM versions ORDER BY version_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def load(self, version_id: int) -> LexiconVersion:
        cached = self._cache.get(version_id)
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT 1 FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise VersionNotFoundError(f"version {version_id} does not exist")
        rows = self._conn.execute(
            "SELECT word, freq FROM entries WHERE version_id = ?", (version_id,)
        ).fetchall()
        version = LexiconVersion.build(
            version_id, (WordEntry(r["word"], int(r["freq"])) for r in rows)
        )
        self._cache[version_id] = version
        return version

    def load_latest(self) -> LexiconVersion | None:
        vid = self.latest_version_id()
        return None if vid is None else self.load(vid)

    # ---- writes --------------------------------------------------------

    def publish(
        self,
        entries: list[WordEntry] | list[dict],
        *,
        note: str = "",
        version_id: int | None = None,
    ) -> LexiconVersion:
        """Atomically publish a complete new version built from ``entries``.

        ``entries`` is the *full* dictionary for the new version (not a diff).
        The version is fully built — normalized, de-duplicated, checksummed —
        *before* the database transaction opens, so an invalid payload never
        leaves a partial version behind.
        """
        prepared: list[WordEntry] = []
        for e in entries:
            if isinstance(e, WordEntry):
                prepared.append(e)
            else:
                prepared.append(WordEntry(str(e["word"]), int(e["freq"])))

        with self._write_lock:
            next_id = self.latest_version_id()
            next_id = 1 if next_id is None else next_id + 1
            if version_id is not None:
                if version_id <= next_id - 1:
                    raise ValueError(
                        f"version_id {version_id} already exists or is in the past"
                    )
                next_id = version_id

            # Build outside SQL transaction; build errors abort cleanly.
            built = LexiconVersion.build(next_id, prepared)

            try:
                with self._conn:  # commits or rolls back atomically
                    now = datetime.now(timezone.utc).isoformat()
                    self._conn.execute(
                        "INSERT INTO versions(version_id, published_at, word_count,"
                        " total_freq, checksum, note) VALUES (?,?,?,?,?,?)",
                        (
                            next_id,
                            now,
                            built.word_count,
                            built.total_freq,
                            built.checksum,
                            note,
                        ),
                    )
                    self._conn.executemany(
                        "INSERT INTO entries(version_id, word, freq) VALUES (?,?,?)",
                        [(next_id, e.word, e.freq) for e in built.entries],
                    )
            except Exception:
                raise
        self._cache[next_id] = built
        return built

    def seed_from_json(self, path: str | Path, *, version_id: int = 1) -> LexiconVersion | None:
        """Load a bundled JSON fixture as the first version if store is empty."""
        if self.latest_version_id() is not None:
            return None
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.publish(payload["words"], note=f"seed:{Path(path).name}", version_id=version_id)
