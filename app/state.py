"""State isolation: SQLite-backed persistence with strict collection scoping.

Two tables hold durable state; every query that touches a share is scoped by
its parent ``collection_id`` and a foreign key, so shares from one collection
can never satisfy a recovery for another.

* ``collections`` - one row per sharing set: identity, threshold/total binding,
  field parameters, the per-collection integrity (HMAC) key and block count.
* ``shares``      - one row per issued share. ``ys`` is stored as JSON text of
  decimal strings; the MAC tag is stored alongside.

The store persists state but makes **no** security decisions; those belong to
:mod:`app.core.kernel` and :mod:`app.parsing`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

from app.core.field import FIELD_VERSION


_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    threshold     INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    field_version TEXT NOT NULL,
    prime         TEXT NOT NULL,
    prime_bits    INTEGER NOT NULL,
    chunk_bytes   INTEGER NOT NULL,
    block_count   INTEGER NOT NULL,
    mac_key       BLOB NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS shares (
    collection_id TEXT NOT NULL,
    x             INTEGER NOT NULL,
    ys            TEXT NOT NULL,
    mac           TEXT NOT NULL,
    PRIMARY KEY (collection_id, x),
    FOREIGN KEY (collection_id) REFERENCES collections(collection_id)
);

CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    TEXT NOT NULL,
    collection_id TEXT,
    action        TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    detail        TEXT,
    fingerprints  TEXT,
    at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_request ON audit(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_collection ON audit(collection_id);
"""


class CollectionNotFound(KeyError):
    pass


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------ #
    def create_collection(
        self,
        *,
        collection_id: str,
        threshold: int,
        total: int,
        block_count: int,
        mac_key: bytes,
        prime: str,
        prime_bits: int = 256,
        chunk_bytes: int = 31,
        field_version: str = FIELD_VERSION,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO collections
                   (collection_id, threshold, total, field_version, prime,
                    prime_bits, chunk_bytes, block_count, mac_key)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (collection_id, threshold, total, field_version, prime,
                 prime_bits, chunk_bytes, block_count, mac_key),
            )

    def get_collection(self, collection_id: str) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
        if row is None:
            raise CollectionNotFound(collection_id)
        return row

    def collection_exists(self, collection_id: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM collections WHERE collection_id = ?",
                (collection_id,),
            ).fetchone() is not None

    def insert_share(self, collection_id: str, x: int, ys, mac: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO shares (collection_id, x, ys, mac) VALUES (?,?,?,?)",
                (collection_id, x, json.dumps([str(v) for v in ys]), mac),
            )

    def list_shares(self, collection_id: str):
        with self._lock:
            return self._conn.execute(
                "SELECT x, ys, mac FROM shares WHERE collection_id = ? ORDER BY x",
                (collection_id,),
            ).fetchall()

    # ---------------- audit --------------------------------------------- #
    def append_audit(
        self,
        *,
        request_id: str,
        collection_id: str | None,
        action: str,
        verdict: str,
        detail: str | None,
        fingerprints,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO audit
                   (request_id, collection_id, action, verdict, detail, fingerprints)
                   VALUES (?,?,?,?,?,?)""",
                (request_id, collection_id, action, verdict, detail,
                 json.dumps(fingerprints or [])),
            )

    def query_audit(
        self,
        *,
        request_id: str | None = None,
        collection_id: str | None = None,
        limit: int = 100,
    ):
        sql = "SELECT * FROM audit WHERE 1=1"
        params: list = []
        if request_id is not None:
            sql += " AND request_id = ?"
            params.append(request_id)
        if collection_id is not None:
            sql += " AND collection_id = ?"
            params.append(collection_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
