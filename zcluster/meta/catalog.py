"""Transactional metadata in SQLite.

The catalog is the single source of truth for what exists on disk.  Arrow
files are *first written*, then published inside a single ``BEGIN IMMEDIATE``
transaction; deletions commit first and files are removed after commit.  That
ordering means a crash can leave orphan files (harmless, cleaned by compact
bookkeeping) but can never leave metadata pointing at a missing file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

CATALOG_FILENAME = "catalog.sqlite"
CATALOG_VERSION = 1


class NotFoundError(KeyError):
    """Requested dataset/request id does not exist."""


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    path: str
    row_count: int
    code_min: int
    code_max: int
    format_version: int
    created_by: str  # ingest request id or "compact:<request id>"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.db_path = os.path.join(self.root, CATALOG_FILENAME)
        # check_same_thread=False: FastAPI executes sync endpoints in a
        # worker thread.  All writes are serialized via BEGIN IMMEDIATE and
        # each statement is short-lived, so the relaxed thread check is safe.
        self._conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        with self.transaction():
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS dataset(
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   name TEXT NOT NULL,
                   dimensions_json TEXT NOT NULL,
                   coder_version INTEGER NOT NULL,
                   chunk_format_version INTEGER NOT NULL,
                   chunk_size INTEGER NOT NULL,
                   next_row_id INTEGER NOT NULL,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL)"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS chunks(
                   chunk_id TEXT PRIMARY KEY,
                   path TEXT NOT NULL,
                   row_count INTEGER NOT NULL,
                   code_min TEXT NOT NULL,
                   code_max TEXT NOT NULL,
                   format_version INTEGER NOT NULL,
                   created_by TEXT NOT NULL,
                   created_at TEXT NOT NULL)"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS audit(
                   request_id TEXT PRIMARY KEY,
                   kind TEXT NOT NULL,
                   status TEXT NOT NULL,
                   summary TEXT NOT NULL,
                   detail_json TEXT NOT NULL,
                   at TEXT NOT NULL)"""
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- dataset ----------------------------------------------------------
    def initialize_dataset(
        self, name: str, dims: list[dict], chunk_size: int,
        coder_version: int, chunk_format_version: int,
    ) -> None:
        row = self._conn.execute("SELECT id FROM dataset WHERE id = 1").fetchone()
        if row is not None:
            raise FileExistsError("dataset already initialized")
        with self.transaction():
            self._conn.execute(
                """INSERT INTO dataset(id, name, dimensions_json, coder_version,
                   chunk_format_version, chunk_size, next_row_id, created_at, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (name, json.dumps(dims), coder_version, chunk_format_version,
                 chunk_size, _now(), _now()),
            )

    def dataset_row(self) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM dataset WHERE id = 1").fetchone()

    def require_dataset(self) -> sqlite3.Row:
        row = self.dataset_row()
        if row is None:
            raise NotFoundError("no dataset initialized; POST /api/schema first")
        return row

    def dimensions(self) -> list[dict]:
        return json.loads(self.require_dataset()["dimensions_json"])

    def next_row_ids(self, count: int) -> range:
        """Reserve ``count`` stable, gap-free row identities."""
        with self.transaction():
            row = self.require_dataset()
            start = row["next_row_id"]
            self._conn.execute(
                "UPDATE dataset SET next_row_id = ?, updated_at = ? WHERE id = 1",
                (start + count, _now()),
            )
        return range(start, start + count)

    # -- chunks -----------------------------------------------------------
    def add_chunks(self, records: list[ChunkRecord]) -> None:
        with self.transaction():
            for r in records:
                self._conn.execute(
                    """INSERT INTO chunks(chunk_id, path, row_count, code_min,
                       code_max, format_version, created_at, created_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r.chunk_id, r.path, r.row_count, str(r.code_min),
                     str(r.code_max), r.format_version, _now(), r.created_by),
                )

    def replace_chunks(
        self, old_ids: list[str], new_records: list[ChunkRecord]
    ) -> list[tuple[str, str]]:
        """Publish rewritten chunks and retire old ones (one transaction).

        Returns ``(chunk_id, path)`` pairs of retired files so the caller can
        unlink them *after* commit — metadata never points at a missing file.
        """
        placeholders = ",".join("?" for _ in old_ids)
        retired: list[tuple[str, str]] = []
        with self.transaction():
            if old_ids:
                rows = self._conn.execute(
                    f"SELECT chunk_id, path FROM chunks WHERE chunk_id IN ({placeholders})",
                    old_ids,
                ).fetchall()
                retired = [(r["chunk_id"], r["path"]) for r in rows]
                self._conn.execute(
                    f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", old_ids
                )
            for r in new_records:
                self._conn.execute(
                    """INSERT INTO chunks(chunk_id, path, row_count, code_min,
                       code_max, format_version, created_at, created_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r.chunk_id, r.path, r.row_count, str(r.code_min),
                     str(r.code_max), r.format_version, _now(), r.created_by),
                )
        return retired

    def list_chunks(self) -> list[ChunkRecord]:
        rows = self._conn.execute(
            "SELECT * FROM chunks ORDER BY code_min ASC, chunk_id ASC"
        ).fetchall()
        return [
            ChunkRecord(
                chunk_id=r["chunk_id"], path=r["path"], row_count=r["row_count"],
                code_min=int(r["code_min"]), code_max=int(r["code_max"]),
                format_version=r["format_version"], created_by=r["created_by"],
            )
            for r in rows
        ]

    def delete_chunk_rows(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        with self.transaction():
            self._conn.execute(
                f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", chunk_ids
            )

    # -- audit ------------------------------------------------------------
    def record_audit(
        self, request_id: str, kind: str, status: str,
        summary: str, detail: dict,
    ) -> None:
        with self.transaction():
            self._conn.execute(
                """INSERT INTO audit(request_id, kind, status, summary,
                   detail_json, at) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET
                     kind=excluded.kind, status=excluded.status,
                     summary=excluded.summary, detail_json=excluded.detail_json,
                     at=excluded.at""",
                (request_id, kind, status, summary, json.dumps(detail), _now()),
            )

    def get_audit(self, request_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM audit WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no request with id {request_id!r}")
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        return d

    def list_audit(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM audit ORDER BY at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
