"""SQLite persistence with version binding.

Schema (one database file per service)::

    documents(
      doc_id TEXT PRIMARY KEY,
      text            TEXT NOT NULL,   -- canonical text
      normalization   TEXT NOT NULL,   -- form used for the canonical text
      text_sha256     TEXT NOT NULL,   -- digest the index version is bound to
      index_blob      BLOB NOT NULL,   -- versioned, checksummed TextIndex
      index_version   TEXT NOT NULL,   -- data identity (grapheme/unicode/blob)
      created_at      TEXT NOT NULL,
      updated_at      TEXT NOT NULL,
      revision        INTEGER NOT NULL -- increments on every edit
    )
    versions(...)                       -- append-only history per document

The digest binds an index *version* to the exact source text: an edit
carries an optional ``base_digest``; mismatch raises
:class:`DigestMismatch` (409) instead of applying to a document the client's
view is stale about.  A blob whose Unicode/blob identity differs from the
running service raises :class:`IndexVersionMismatch`; checksum/structural
failures raise :class:`IndexCorrupt`.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from . import digest as digest_mod
from . import index as index_mod
from .errors import (
    DocumentAlreadyExists,
    DocumentNotFound,
    IndexCorrupt,
    StorageFull,
)
from .unicode_version import DATA_VERSION_IDENTITY


@dataclass(frozen=True)
class StoredDocument:
    doc_id: str
    text: str
    normalization: str
    text_sha256: str
    revision: int
    index: index_mod.TextIndex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    normalization TEXT NOT NULL,
    text_sha256   TEXT NOT NULL,
    index_blob    BLOB NOT NULL,
    index_version TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    revision      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    text_sha256 TEXT NOT NULL,
    index_version TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(doc_id, revision)
);
"""

# SQLite primary/extended result codes that mean "no space / too big".
_SQLITE_FULL_CODES = {13, 18, 2000, 2001}  # FULL, TOOBIG, CANTOPEN full-ish


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        # check_same_thread=False: the ASGI server dispatches requests on
        # worker threads. Access is serialized by SQLite's own locking plus
        # our short transactions (autocommit=False implicit; each mutation
        # runs inside `with self.conn`).
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.conn:
            self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- basic ops ----------------------------------------------------------

    def exists(self, doc_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return row is not None

    def create(
        self,
        doc_id: str,
        text: str,
        normalization: str,
        idx: index_mod.TextIndex,
    ) -> StoredDocument:
        if self.exists(doc_id):
            raise DocumentAlreadyExists(doc_id)
        raw = text.encode("utf-8")
        text_digest = digest_mod.digest_utf8(raw)
        blob = index_mod.pack(idx)
        ts = _now()
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """INSERT INTO documents(doc_id, text, normalization,
                          text_sha256, index_blob, index_version,
                          created_at, updated_at, revision)
                       VALUES (?,?,?,?,?,?,?,?,0)""",
                    (doc_id, text, normalization, text_digest, blob,
                     DATA_VERSION_IDENTITY, ts, ts),
                )
                self.conn.execute(
                    """INSERT INTO versions(doc_id, revision, text_sha256,
                          index_version, created_at)
                       VALUES (?,?,?,?,?)""",
                    (doc_id, 0, text_digest, DATA_VERSION_IDENTITY, ts),
                )
        except sqlite3.OperationalError as exc:
            raise self._map_sqlite(exc) from None
        return self.get(doc_id)

    def get(self, doc_id: str) -> StoredDocument:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        if row is None:
            raise DocumentNotFound(doc_id)
        idx = self._load_index(row)
        return StoredDocument(
            doc_id=doc_id,
            text=row["text"],
            normalization=row["normalization"],
            text_sha256=row["text_sha256"],
            revision=row["revision"],
            index=idx,
        )

    def replace(
        self,
        doc_id: str,
        text: str,
        idx: index_mod.TextIndex,
        *,
        expected_digest: str | None = None,
    ) -> StoredDocument:
        """Overwrite document text+index, bumping revision; append a version."""
        row = self.conn.execute(
            "SELECT revision, text_sha256 FROM documents WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise DocumentNotFound(doc_id)
        if expected_digest is not None and expected_digest != row["text_sha256"]:
            from .errors import DigestMismatch
            raise DigestMismatch(expected_digest, row["text_sha256"])

        text_digest = digest_mod.digest_utf8(text.encode("utf-8"))
        blob = index_mod.pack(idx)
        ts = _now()
        new_revision = row["revision"] + 1
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    """UPDATE documents SET text=?, text_sha256=?,
                          index_blob=?, index_version=?, updated_at=?,
                          revision=? WHERE doc_id=?""",
                    (text, text_digest, blob, DATA_VERSION_IDENTITY, ts,
                     new_revision, doc_id),
                )
                self.conn.execute(
                    """INSERT INTO versions(doc_id, revision, text_sha256,
                          index_version, created_at)
                       VALUES (?,?,?,?,?)""",
                    (doc_id, new_revision, text_digest,
                     DATA_VERSION_IDENTITY, ts),
                )
        except sqlite3.OperationalError as exc:
            raise self._map_sqlite(exc) from None
        return self.get(doc_id)

    def delete(self, doc_id: str) -> None:
        if not self.exists(doc_id):
            raise DocumentNotFound(doc_id)
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM versions WHERE doc_id=?", (doc_id,))
            self.conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))

    def list_documents(self) -> list[dict[str, str]]:
        rows = self.conn.execute(
            """SELECT doc_id, normalization, text_sha256, revision, updated_at
               FROM documents ORDER BY doc_id"""
        ).fetchall()
        return [dict(r) for r in rows]

    def list_versions(self, doc_id: str) -> list[dict[str, str]]:
        if not self.exists(doc_id):
            raise DocumentNotFound(doc_id)
        rows = self.conn.execute(
            """SELECT revision, text_sha256, index_version, created_at
               FROM versions WHERE doc_id=? ORDER BY revision""",
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- internals -----------------------------------------------------------

    def _load_index(self, row: sqlite3.Row) -> index_mod.TextIndex:
        stored_version = row["index_version"]
        if stored_version != DATA_VERSION_IDENTITY:
            from .errors import IndexVersionMismatch
            raise IndexVersionMismatch(stored_version, DATA_VERSION_IDENTITY)
        try:
            return index_mod.unpack(row["index_blob"], text=row["text"])
        except IndexCorrupt:
            raise
        except IndexVersionMismatch:
            raise

    @staticmethod
    def _map_sqlite(exc: sqlite3.OperationalError) -> Exception:
        code = exc.sqlite_errorcode if hasattr(exc, "sqlite_errorcode") else None
        if code in _SQLITE_FULL_CODES or "disk" in str(exc).lower():
            return StorageFull(str(exc), code)
        return exc


@contextmanager
def open_storage(path: str) -> Iterator[Storage]:
    store = Storage(path)
    try:
        yield store
    finally:
        store.close()
