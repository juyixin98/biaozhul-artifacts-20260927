"""Indexed storage (SQLite).

Responsibilities:
  * persistent, indexed storage of accepted headers and committees
  * a single ``meta`` row-set describing the current trusted tip
  * an append-only audit log of every kernel decision

The store enforces *no protocol rules*; the kernel validates first and only
then asks the store to persist. Every acceptance is applied inside one
SQLite transaction, so a crash/rejection can never leave a half-written tip.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import codec
from .types import Certificate, Committee, Header

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS committees (
    committee_id  TEXT PRIMARY KEY,
    epoch         INTEGER NOT NULL,
    quorum_weight INTEGER NOT NULL,
    total_weight  INTEGER NOT NULL,
    member_count  INTEGER NOT NULL,
    wire          BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS headers (
    digest            TEXT PRIMARY KEY,
    height            INTEGER NOT NULL,
    round             INTEGER NOT NULL,
    epoch             INTEGER NOT NULL,
    timestamp         INTEGER NOT NULL,
    parent_digest     TEXT NOT NULL,
    next_committee_id TEXT,
    wire              BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_headers_height ON headers(height);
CREATE INDEX IF NOT EXISTS idx_headers_parent ON headers(parent_digest);
CREATE TABLE IF NOT EXISTS certificates (
    header_digest     TEXT PRIMARY KEY,
    signed_weight     INTEGER NOT NULL,
    participant_count INTEGER NOT NULL,
    wire              BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    at_unix     REAL NOT NULL,
    action      TEXT NOT NULL,
    result      TEXT NOT NULL,
    error_code  TEXT,
    error_category TEXT,
    detail      TEXT NOT NULL
);
"""

META_INITIALIZED = "initialized"
META_CHAIN_ID = "chain_id"
META_TIP_DIGEST = "tip_digest"
META_TIP_HEIGHT = "tip_height"
META_TIP_ROUND = "tip_round"
META_TIP_EPOCH = "tip_epoch"
META_TIP_TS = "tip_timestamp"
META_PENDING_CID = "pending_committee_id"
META_PENDING_EPOCH = "pending_epoch"
META_TRUST_PERIOD = "trust_period_seconds"
META_CHECKPOINT_KEY = "checkpoint_key_hex"


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the HTTP service may serve requests from
        # worker threads. SQLite serializes access with its own locking plus
        # our short, fully-synchronous transactions; writes are not shared
        # between processes (file store) which is the intended deployment.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._tx_lock = __import__("threading").RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------- meta

    def meta_get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["v"])

    def is_initialized(self) -> bool:
        return bool(self.meta_get(META_INITIALIZED, False))

    def snapshot_meta(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT k, v FROM meta").fetchall()
        return {r["k"]: json.loads(r["v"]) for r in rows}

    # ------------------------------------------------------- committees

    def has_committee(self, cid: bytes) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM committees WHERE committee_id=?", (cid.hex(),)
        ).fetchone() is not None

    def put_committee(self, conn: sqlite3.Connection, c: Committee) -> None:
        cid = codec.committee_id(c)
        conn.execute(
            "INSERT OR IGNORE INTO committees VALUES (?,?,?,?,?,?)",
            (
                cid.hex(),
                c.epoch,
                c.quorum_weight,
                c.total_weight,
                len(c.members),
                codec.encode_committee(c),
            ),
        )

    def get_committee(self, cid: bytes) -> Committee | None:
        row = self._conn.execute(
            "SELECT wire FROM committees WHERE committee_id=?", (cid.hex(),)
        ).fetchone()
        if row is None:
            return None
        return codec.decode_committee(row["wire"])

    def get_committee_for_epoch(self, epoch: int) -> Committee | None:
        row = self._conn.execute(
            "SELECT wire FROM committees WHERE epoch=? ORDER BY rowid LIMIT 1",
            (epoch,),
        ).fetchone()
        if row is None:
            return None
        return codec.decode_committee(row["wire"])

    # ---------------------------------------------------------- headers

    def has_header(self, digest: bytes) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM headers WHERE digest=?", (digest.hex(),)
        ).fetchone() is not None

    def get_header(self, digest: bytes) -> Header | None:
        row = self._conn.execute(
            "SELECT wire FROM headers WHERE digest=?", (digest.hex(),)
        ).fetchone()
        if row is None:
            return None
        return codec.decode_header(row["wire"])

    def get_header_by_height(self, height: int) -> Header | None:
        row = self._conn.execute(
            "SELECT wire FROM headers WHERE height=? ORDER BY rowid LIMIT 1",
            (height,),
        ).fetchone()
        if row is None:
            return None
        return codec.decode_header(row["wire"])

    def all_header_digests(self) -> set[str]:
        return {
            r[0]
            for r in self._conn.execute("SELECT digest FROM headers").fetchall()
        }

    def all_committee_ids(self) -> set[str]:
        return {
            r[0]
            for r in self._conn.execute("SELECT committee_id FROM committees").fetchall()
        }

    # --------------------------------------------------------- bootstrap

    def commit_bootstrap(
        self,
        *,
        chain_id: str,
        header: Header,
        committee: Committee,
        trust_period_seconds: int,
        checkpoint_key: bytes,
    ) -> None:
        digest = codec.header_digest(header)
        cid = codec.committee_id(committee)
        meta = {
            META_INITIALIZED: True,
            META_CHAIN_ID: chain_id,
            META_TIP_DIGEST: digest.hex(),
            META_TIP_HEIGHT: header.height,
            META_TIP_ROUND: header.round,
            META_TIP_EPOCH: header.epoch,
            META_TIP_TS: header.timestamp,
            META_PENDING_CID: None,
            META_PENDING_EPOCH: None,
            META_TRUST_PERIOD: trust_period_seconds,
            META_CHECKPOINT_KEY: checkpoint_key.hex(),
        }
        with self._tx_lock, self._conn:  # one transaction
            self.put_committee(self._conn, committee)
            self._conn.execute(
                "INSERT OR REPLACE INTO headers VALUES (?,?,?,?,?,?,?,?)",
                (
                    digest.hex(),
                    header.height,
                    header.round,
                    header.epoch,
                    header.timestamp,
                    header.parent_digest.hex(),
                    cid.hex() if header.next_committee is not None else None,
                    codec.encode_header(header),
                ),
            )
            for k, v in meta.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )

    # ------------------------------------------------- header acceptance

    def commit_header(
        self,
        *,
        header: Header,
        cert: Certificate,
        signed_weight: int,
        participant_count: int,
        next_committee_id_hex: str | None,
        pending_cid_hex: str | None,
        pending_epoch: int | None,
    ) -> bytes:
        """Persist one fully-verified header and advance the tip atomically."""
        digest = codec.header_digest(header)
        with self._tx_lock, self._conn:
            if header.next_committee is not None:
                self.put_committee(self._conn, header.next_committee)
            self._conn.execute(
                "INSERT OR REPLACE INTO headers VALUES (?,?,?,?,?,?,?,?)",
                (
                    digest.hex(),
                    header.height,
                    header.round,
                    header.epoch,
                    header.timestamp,
                    header.parent_digest.hex(),
                    next_committee_id_hex,
                    codec.encode_header(header),
                ),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO certificates VALUES (?,?,?,?)",
                (
                    digest.hex(),
                    signed_weight,
                    participant_count,
                    codec.encode_certificate(cert),
                ),
            )
            tip_meta = {
                META_TIP_DIGEST: digest.hex(),
                META_TIP_HEIGHT: header.height,
                META_TIP_ROUND: header.round,
                META_TIP_EPOCH: header.epoch,
                META_TIP_TS: header.timestamp,
                META_PENDING_CID: pending_cid_hex,
                META_PENDING_EPOCH: pending_epoch,
            }
            for k, v in tip_meta.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )
        return digest

    # ------------------------------------------------------------- audit

    def add_audit(
        self,
        *,
        run_id: str | None,
        at_unix: float,
        action: str,
        result: str,
        error_code: str | None,
        error_category: str | None,
        detail: dict[str, Any],
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO audit(run_id, at_unix, action, result, error_code, "
            "error_category, detail) VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                at_unix,
                action,
                result,
                error_code,
                error_category,
                json.dumps(detail, sort_keys=True, default=str),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "at_unix": r["at_unix"],
                "action": r["action"],
                "result": r["result"],
                "error_code": r["error_code"],
                "error_category": r["error_category"],
                "detail": json.loads(r["detail"]),
            }
            for r in rows
        ]
