"""SQLite-backed metadata catalog with explicit transaction boundaries.

Tables
------
meta(key TEXT PRIMARY KEY, value TEXT)
    engine version, schema definition JSON.
counters(name TEXT PRIMARY KEY, value INTEGER)
    ``next_row_id`` and ``next_chunk_id`` - monotonically increasing, never
    reused (rewrites allocate fresh chunk ids; row ids are preserved on the
    rows themselves).
chunks(
    chunk_id INTEGER PRIMARY KEY,
    schema  TEXT NOT NULL,
    path    TEXT NOT NULL,
    num_rows INTEGER NOT NULL,
    min_code TEXT NOT NULL,   -- decimal string, codes may reach 128 bit
    max_code TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
request_log(
    request_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    kind TEXT, schema TEXT, payload TEXT,
    status TEXT, http_status INTEGER,
    num_results INTEGER, candidates INTEGER, chunks_read INTEGER,
    io_bytes INTEGER, uncertainty TEXT
)

Every multi-row mutation happens inside an IMMEDIATE transaction. The file is
created with WAL journaling and foreign-key safety; concurrent local readers
never block a writer's commit view.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import ENGINE_VERSION, __version__
from .encoding import SchemaSpec
from .errors import SchemaNotFound

CATALOG_VERSION_KEY = "catalog_version"
SCHEMA_KEY_PREFIX = "schema:"
CATALOG_VERSION = 1


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: int
    schema: str
    path: str
    num_rows: int
    min_code: int
    max_code: int
    byte_size: int


@dataclass(frozen=True)
class Reservation:
    row_ids: tuple[int, ...]
    chunk_id: int


class Catalog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._tx() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id INTEGER PRIMARY KEY,
                    schema TEXT NOT NULL,
                    path TEXT NOT NULL,
                    num_rows INTEGER NOT NULL,
                    min_code TEXT NOT NULL,
                    max_code TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )"""
            )
            cur.execute(
                """CREATE TABLE IF NOT EXISTS request_log (
                    request_id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    kind TEXT, schema TEXT, payload TEXT,
                    status TEXT, http_status INTEGER,
                    num_results INTEGER, candidates INTEGER,
                    chunks_read INTEGER, io_bytes INTEGER,
                    uncertainty TEXT
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_schema ON chunks(schema)")
            row = cur.execute(
                "SELECT value FROM meta WHERE key=?", (CATALOG_VERSION_KEY,)
            ).fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?)",
                    (CATALOG_VERSION_KEY, json.dumps({
                        "version": CATALOG_VERSION,
                        "package": __version__,
                        "engine": ENGINE_VERSION,
                    })),
                )
            for name in ("next_row_id", "next_chunk_id"):
                cur.execute(
                    "INSERT OR IGNORE INTO counters(name, value) VALUES(?, 0)", (name,)
                )

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.cursor()
            yield cur
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ schema
    def put_schema(self, spec: SchemaSpec, *, overwrite: bool = False) -> None:
        key = f"{SCHEMA_KEY_PREFIX}{spec.name}"
        payload = json.dumps(spec.to_dict(), sort_keys=True)
        with self._tx() as cur:
            exists = cur.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone()
            if exists and not overwrite:
                from .errors import SchemaExists

                raise SchemaExists(f"schema {spec.name!r} already exists", schema=spec.name)
            cur.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, payload),
            )

    def get_schema(self, name: str) -> SchemaSpec:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"{SCHEMA_KEY_PREFIX}{name}",)
        ).fetchone()
        if row is None:
            raise SchemaNotFound(f"schema {name!r} not found", schema=name)
        return SchemaSpec.from_dict(json.loads(row["value"]))

    def list_schemas(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT key FROM meta WHERE key LIKE ? ORDER BY key",
            (f"{SCHEMA_KEY_PREFIX}%",),
        ).fetchall()
        return [r["key"][len(SCHEMA_KEY_PREFIX):] for r in rows]

    def delete_schema_metadata(self, name: str) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM chunks WHERE schema=?", (name,))
            cur.execute("DELETE FROM meta WHERE key=?", (f"{SCHEMA_KEY_PREFIX}{name}",))

    # -------------------------------------------------------------- counters /
    def reserve_ids(self, n_rows: int) -> Reservation:
        """Atomically reserve one chunk id and ``n_rows`` stable row ids."""
        if n_rows <= 0:
            raise ValueError("must reserve at least one row id")
        with self._tx() as cur:
            row = cur.execute(
                "SELECT value FROM counters WHERE name='next_row_id'"
            ).fetchone()
            first_rid = int(row["value"])
            cur.execute(
                "UPDATE counters SET value=? WHERE name='next_row_id'", (first_rid + n_rows,)
            )
            row = cur.execute(
                "SELECT value FROM counters WHERE name='next_chunk_id'"
            ).fetchone()
            chunk_id = int(row["value"])
            cur.execute(
                "UPDATE counters SET value=? WHERE name='next_chunk_id'", (chunk_id + 1,)
            )
        return Reservation(tuple(range(first_rid, first_rid + n_rows)), chunk_id)

    def reserve_chunk_ids(self, n: int) -> list[int]:
        """Reserve ``n`` fresh chunk ids without consuming row ids."""
        if n <= 0:
            return []
        with self._tx() as cur:
            row = cur.execute(
                "SELECT value FROM counters WHERE name='next_chunk_id'"
            ).fetchone()
            first = int(row["value"])
            cur.execute(
                "UPDATE counters SET value=? WHERE name='next_chunk_id'", (first + n,)
            )
        return list(range(first, first + n))

    # ------------------------------------------------------------------ chunks
    def register_chunk(self, record: ChunkRecord) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO chunks(chunk_id, schema, path, num_rows, min_code, max_code, byte_size)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    record.chunk_id,
                    record.schema,
                    record.path,
                    record.num_rows,
                    str(record.min_code),
                    str(record.max_code),
                    record.byte_size,
                ),
            )

    def replace_chunks(
        self,
        schema: str,
        old_chunk_ids: list[int],
        new_records: list[ChunkRecord],
    ) -> None:
        """Publish a rewrite: register new chunks and retire old ones atomically."""
        with self._tx() as cur:
            for rec in new_records:
                cur.execute(
                    """INSERT INTO chunks(chunk_id, schema, path, num_rows, min_code, max_code, byte_size)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        rec.chunk_id,
                        rec.schema,
                        rec.path,
                        rec.num_rows,
                        str(rec.min_code),
                        str(rec.max_code),
                        rec.byte_size,
                    ),
                )
            if old_chunk_ids:
                cur.executemany(
                    "DELETE FROM chunks WHERE chunk_id=? AND schema=?",
                    [(cid, schema) for cid in old_chunk_ids],
                )

    def list_chunks(self, schema: str) -> list[ChunkRecord]:
        rows = self._conn.execute(
            "SELECT * FROM chunks WHERE schema=? ORDER BY chunk_id", (schema,)
        ).fetchall()
        return [
            ChunkRecord(
                chunk_id=r["chunk_id"],
                schema=r["schema"],
                path=r["path"],
                num_rows=r["num_rows"],
                min_code=int(r["min_code"]),
                max_code=int(r["max_code"]),
                byte_size=r["byte_size"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------- request log
    def log_request(self, entry: dict) -> None:
        cols = (
            "request_id", "kind", "schema", "payload", "status", "http_status",
            "num_results", "candidates", "chunks_read", "io_bytes", "uncertainty",
        )
        values = [entry.get(c) for c in cols]
        placeholders = ",".join("?" for _ in cols)
        with self._tx() as cur:
            cur.execute(
                f"INSERT INTO request_log({','.join(cols)}) VALUES({placeholders})",
                values,
            )

    def get_request(self, request_id: str) -> dict | None:
        r = self._conn.execute(
            "SELECT * FROM request_log WHERE request_id=?", (request_id,)
        ).fetchone()
        return dict(r) if r else None

    def list_requests(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM request_log ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
