"""SQLite-backed version storage.

The store keeps the full provenance graph required to answer *where did a
merged document come from*:

``documents``
    One row per logical document under collaboration.
``versions``
    Immutable, content-addressed (sha256) text versions with a role
    (``base``/``local``/``remote``/``merged``) and an optional parent.
``merges``
    One merge attempt per request, with status ``auto`` or ``conflict`` and
    a pointer to the produced merged version once rebuilt.
``conflicts``
    Every conflict block of a conflicted merge, including exact three-way
    source ranges and the materialized alternatives.
``resolutions``
    The explicit choice recorded for each conflict when rebuilding.

The store is deliberately ignorant of merge policy: it persists inputs and
outcomes; the engine in :mod:`merge3.merge` decides what they mean.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import contextlib
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version_id  TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    role        TEXT NOT NULL CHECK (role IN ('base','local','remote','merged')),
    content     TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    char_count  INTEGER NOT NULL,
    parent_version_id TEXT REFERENCES versions(version_id),
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS merges (
    merge_id          TEXT PRIMARY KEY,
    request_id        TEXT NOT NULL,
    document_id       TEXT NOT NULL REFERENCES documents(document_id),
    base_version_id   TEXT NOT NULL REFERENCES versions(version_id),
    local_version_id  TEXT NOT NULL REFERENCES versions(version_id),
    remote_version_id TEXT NOT NULL REFERENCES versions(version_id),
    merged_version_id TEXT REFERENCES versions(version_id),
    status            TEXT NOT NULL CHECK (status IN ('auto','conflict','rebuilt','rejected')),
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_merges_document ON merges(document_id);
CREATE INDEX IF NOT EXISTS idx_merges_request  ON merges(request_id);

CREATE TABLE IF NOT EXISTS conflicts (
    merge_id      TEXT NOT NULL REFERENCES merges(merge_id) ON DELETE CASCADE,
    conflict_id   TEXT NOT NULL,
    conflict_type TEXT NOT NULL,
    base_region   TEXT NOT NULL,
    local_region  TEXT NOT NULL,
    remote_region TEXT NOT NULL,
    base_text     TEXT NOT NULL,
    local_text    TEXT NOT NULL,
    remote_text   TEXT NOT NULL,
    local_edit_ids  TEXT NOT NULL,
    remote_edit_ids TEXT NOT NULL,
    allowed_resolutions TEXT NOT NULL,
    PRIMARY KEY (merge_id, conflict_id)
);

CREATE TABLE IF NOT EXISTS resolutions (
    merge_id    TEXT NOT NULL REFERENCES merges(merge_id) ON DELETE CASCADE,
    conflict_id TEXT NOT NULL,
    choice      TEXT NOT NULL,
    custom_text TEXT,
    decided_at  REAL NOT NULL,
    PRIMARY KEY (merge_id, conflict_id)
);
"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VersionStore:
    """Synchronous SQLite repository (one connection; safe across threads via
    check_same_thread=False only when externally serialized — the API layer
    opens per-request connections)."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            import os
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- documents & versions --------------------------------------------- #

    def create_document(self, document_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO documents(document_id, created_at) VALUES (?, ?)",
                (document_id, time.time()),
            )

    def ensure_document(self, document_id: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            self.create_document(document_id)

    def add_version(self, document_id: str, role: str, content: str,
                    version_id: Optional[str] = None,
                    parent_version_id: Optional[str] = None) -> str:
        if role not in ("base", "local", "remote", "merged"):
            raise ValueError(f"bad role {role!r}")
        digest = sha256_text(content)
        if version_id is None:
            version_id = f"{role}_{digest[:16]}"
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO versions(version_id, document_id, role, content,
                       sha256, char_count, parent_version_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(version_id) DO NOTHING""",
                (version_id, document_id, role, content, digest,
                 len(content), parent_version_id, time.time()),
            )
        return version_id

    def get_version(self, version_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_versions(self, document_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT version_id, role, sha256, char_count, parent_version_id, "
            "created_at FROM versions WHERE document_id = ? ORDER BY created_at",
            (document_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- merges ------------------------------------------------------------ #

    def record_merge(self, *, merge_id: str, request_id: str, document_id: str,
                     base_version_id: str, local_version_id: str,
                     remote_version_id: str, status: str,
                     merged_version_id: Optional[str] = None,
                     conflicts: Optional[list[dict[str, Any]]] = None) -> None:
        if status not in ("auto", "conflict", "rebuilt", "rejected"):
            raise ValueError(f"bad status {status!r}")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO merges(merge_id, request_id, document_id,
                       base_version_id, local_version_id, remote_version_id,
                       merged_version_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (merge_id, request_id, document_id, base_version_id,
                 local_version_id, remote_version_id, merged_version_id,
                 status, time.time()),
            )
            for block in conflicts or []:
                conn.execute(
                    """INSERT INTO conflicts(merge_id, conflict_id, conflict_type,
                           base_region, local_region, remote_region,
                           base_text, local_text, remote_text,
                           local_edit_ids, remote_edit_ids, allowed_resolutions)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (merge_id, block["conflict_id"], block["conflict_type"],
                     json.dumps(block["base_region"]),
                     json.dumps(block["local_region"]),
                     json.dumps(block["remote_region"]),
                     block["base_text"], block["local_text"], block["remote_text"],
                     json.dumps(block["local_edit_ids"]),
                     json.dumps(block["remote_edit_ids"]),
                     json.dumps(block["allowed_resolutions"])),
                )

    def attach_merged_version(self, merge_id: str, merged_version_id: str,
                              status: str = "rebuilt") -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE merges SET merged_version_id = ?, status = ? "
                "WHERE merge_id = ?",
                (merged_version_id, status, merge_id),
            )

    def record_resolution(self, merge_id: str, conflict_id: str, choice: str,
                          custom_text: Optional[str]) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO resolutions(merge_id, conflict_id, choice,
                       custom_text, decided_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(merge_id, conflict_id) DO UPDATE SET
                       choice = excluded.choice,
                       custom_text = excluded.custom_text,
                       decided_at = excluded.decided_at""",
                (merge_id, conflict_id, choice, custom_text, time.time()),
            )

    def get_merge(self, merge_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM merges WHERE merge_id = ?", (merge_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_conflicts(self, merge_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM conflicts WHERE merge_id = ? ORDER BY conflict_id",
            (merge_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for key in ("base_region", "local_region", "remote_region",
                        "local_edit_ids", "remote_edit_ids",
                        "allowed_resolutions"):
                d[key] = json.loads(d[key])
            out.append(d)
        return out

    def get_resolutions(self, merge_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT conflict_id, choice, custom_text, decided_at "
            "FROM resolutions WHERE merge_id = ? ORDER BY conflict_id",
            (merge_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_merges(self, document_id: Optional[str] = None) -> list[dict[str, Any]]:
        if document_id:
            rows = self._conn.execute(
                "SELECT merge_id, request_id, document_id, status, "
                "merged_version_id, created_at FROM merges "
                "WHERE document_id = ? ORDER BY created_at",
                (document_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT merge_id, request_id, document_id, status, "
                "merged_version_id, created_at FROM merges ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]
