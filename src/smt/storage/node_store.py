"""SQLite-backed persistence: content-addressed nodes + revision log.

Tables
------
nodes         node_hash (BLOB PK) -> blob          (the trie content store)
revisions     seq (PK), root, created_at, note     (every committed root)
journal       seq (PK), kind, key_hex, value_hex,  (signed append-only log)
              prev_root, new_root, payload_json,
              signature_hex, created_at
meta          key -> value (spec version, schema version)

Nodes are never deleted: proofs verified against an *old* root keep working
because every blob they reference is immutable and still present.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import List, Optional, Tuple

from ..crypto.encoding import canonical_json
from ..kernel.store import MissingNodeError

SCHEMA_VERSION = 1
SPEC_VERSION = "smt-v1"


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class SqliteNodeStore:
    """NodeStore protocol over SQLite.

    FastAPI dispatches synchronous routes on a thread pool, so each thread
    gets its own connection (sqlite3 cursors must not be shared across
    threads concurrently). WAL mode allows concurrent readers with one
    writer; writers are additionally serialized by StateService.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._write_lock = threading.Lock()
        self._local = threading.local()
        init_conn = _connect(path)
        try:
            self._init_schema(init_conn)
        finally:
            init_conn.close()

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    node_hash BLOB PRIMARY KEY,
                    blob      BLOB NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS revisions (
                    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                    root       BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    note       TEXT
                );
                CREATE TABLE IF NOT EXISTS journal (
                    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind          TEXT NOT NULL CHECK (kind IN ('set','delete')),
                    key_hex       TEXT NOT NULL,
                    value_hex     TEXT,
                    prev_root     TEXT NOT NULL,
                    new_root      TEXT NOT NULL,
                    payload_json  TEXT NOT NULL,
                    signature_hex TEXT NOT NULL,
                    created_at    TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_journal_key ON journal(key_hex);
                CREATE INDEX IF NOT EXISTS idx_revisions_root ON revisions(root);
                """
            )
            row = conn.execute(
                "SELECT value FROM meta WHERE key='spec_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('spec_version', ?)", (SPEC_VERSION,)
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
                )
            elif row[0] != SPEC_VERSION:
                raise RuntimeError(
                    f"database spec {row[0]!r} does not match build {SPEC_VERSION!r}"
                )

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _connect(self.path)
            self._local.conn = conn
        return conn

    # ----- NodeStore protocol -----
    def get_node(self, node_hash: bytes) -> Optional[bytes]:
        cur = self._conn.execute("SELECT blob FROM nodes WHERE node_hash=?", (node_hash,))
        row = cur.fetchone()
        return None if row is None else row[0]

    def require_node(self, node_hash: bytes) -> bytes:
        blob = self.get_node(node_hash)
        if blob is None:
            raise MissingNodeError(node_hash.hex())
        return blob

    def put_node(self, blob: bytes) -> bytes:
        from ..crypto.hashing import sha256

        node_hash = sha256(blob)
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO nodes(node_hash, blob) VALUES (?, ?)",
                (node_hash, blob),
            )
        return node_hash

    def put_many(self, blobs: List[bytes]) -> int:
        from ..crypto.hashing import sha256

        rows = [(sha256(b), b) for b in blobs]
        with self._write_lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO nodes(node_hash, blob) VALUES (?, ?)", rows
            )
        return len(rows)

    def node_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    # ----- revisions -----
    def latest_revision(self) -> Optional[Tuple[int, bytes]]:
        row = self._conn.execute(
            "SELECT seq, root FROM revisions ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def record_revision(self, root: bytes, note: str = "") -> int:
        with self._write_lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO revisions(root, created_at, note) VALUES (?,?,?)",
                (root, _now(), note),
            )
            return int(cur.lastrowid)

    def list_revisions(self, limit: int = 100) -> List[dict]:
        rows = self._conn.execute(
            "SELECT seq, root, created_at, note FROM revisions ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"seq": s, "root": r.hex(), "created_at": c, "note": n or ""}
            for s, r, c, n in rows
        ]

    def get_revision_root(self, seq: int) -> Optional[bytes]:
        row = self._conn.execute("SELECT root FROM revisions WHERE seq=?", (seq,)).fetchone()
        return None if row is None else row[0]

    # ----- journal -----
    def append_journal(
        self,
        *,
        kind: str,
        key_hex: str,
        value_hex: Optional[str],
        prev_root: bytes,
        new_root: bytes,
        payload: dict,
        signature_hex: str,
    ) -> int:
        with self._write_lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO journal(kind, key_hex, value_hex, prev_root, new_root,
                                    payload_json, signature_hex, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    kind,
                    key_hex,
                    value_hex,
                    prev_root.hex(),
                    new_root.hex(),
                    canonical_json(payload).decode("utf-8"),
                    signature_hex,
                    _now(),
                ),
            )
            return int(cur.lastrowid)

    def journal_rows(self, after_seq: int = 0) -> List[dict]:
        rows = self._conn.execute(
            """
            SELECT seq, kind, key_hex, value_hex, prev_root, new_root,
                   payload_json, signature_hex, created_at
            FROM journal WHERE seq > ? ORDER BY seq ASC
            """,
            (after_seq,),
        ).fetchall()
        return [
            {
                "seq": s,
                "kind": k,
                "key_hex": kh,
                "value_hex": vh,
                "prev_root": pr,
                "new_root": nr,
                "payload": json.loads(pj),
                "signature_hex": sig,
                "created_at": c,
            }
            for s, k, kh, vh, pr, nr, pj, sig, c in rows
        ]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def _now() -> str:
    # UTC, second precision is enough for the synthetic fixtures.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
