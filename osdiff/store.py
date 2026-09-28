"""SQLite-backed persistent state with per-run isolation.

Every persisted row belongs to a ``run_id``; reads are always scoped by it, so
two concurrent/serial analyses can never see or overwrite each other's state.
A workspace is one SQLite file (or an in-memory database for tests).  The
signing public key is stored at workspace creation and refuses silent
replacement -- that is how a swapped signing key becomes visible.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .signing import (
    load_public_pem,
    public_fingerprint,
    public_pem,
)
from .types import Failure


class StoreError(Exception):
    def __init__(self, code: Failure, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_versions (
    version_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    document TEXT NOT NULL,
    first_seen_run TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    old_version_id TEXT,
    new_version_id TEXT,
    status TEXT NOT NULL,            -- 'complete' | 'failed'
    result_json TEXT,
    failure_code TEXT,
    failure_message TEXT,
    failure_details TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_witnesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    category TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    old_verdict TEXT NOT NULL,
    new_verdict TEXT NOT NULL,
    UNIQUE(run_id, ordinal)
);
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    request_id TEXT,
    stage TEXT NOT NULL,
    caller_location TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    prev_hash TEXT,
    entry_hash TEXT,
    signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_witnesses_run ON run_witnesses(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_events(request_id);
"""


class Store:
    def __init__(self, path: str | Path | None = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe under the single writer lock below.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- workspace identity -------------------------------------------------
    def init_signing_key(self, private_key: Any) -> str:
        """Register the public key. Fails loudly if a different key is present."""
        pem = public_pem(private_key).decode("ascii")
        fp = public_fingerprint(private_key)
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key='public_key_pem'").fetchone()
            if row is not None and row["value"] != pem:
                existing_fp = public_fingerprint(load_public_pem(row["value"].encode()))
                raise StoreError(
                    Failure.BAD_SIGNATURE,
                    f"workspace already bound to signing key {existing_fp}; refusing silent replacement by {fp}",
                )
            if row is None:
                self._conn.execute("INSERT INTO meta(key, value) VALUES('public_key_pem', ?)", (pem,))
                self._conn.execute("INSERT INTO meta(key, value) VALUES('public_key_fp', ?)", (fp,))
                self._conn.commit()
        return fp

    def public_key(self) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key='public_key_pem'").fetchone()
        if row is None:
            raise StoreError(Failure.BAD_SIGNATURE, "workspace has no registered signing key")
        return load_public_pem(row["value"].encode())

    # ---- policy versions ----------------------------------------------------
    def upsert_policy_version(self, version_id: str, fingerprint: str, doc: dict[str, Any], run_id: str, created_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO policy_versions(version_id, fingerprint, document, first_seen_run, created_at)"
                " VALUES(?,?,?,?,?)",
                (version_id, fingerprint, json.dumps(doc, sort_keys=True, ensure_ascii=False), run_id, created_at),
            )
            self._conn.commit()

    def get_policy_version(self, version_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM policy_versions WHERE version_id=?", (version_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---- runs ---------------------------------------------------------------
    def save_run(self, result: Any) -> None:
        from .evidence import request_identity

        summary = result.summary()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, old_version_id, new_version_id, status,"
                " result_json, failure_code, failure_message, failure_details, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    result.run_id, result.old_version_id, result.new_version_id, "complete",
                    json.dumps(summary, ensure_ascii=False), None, None, None, result.created_at,
                ),
            )
            self._conn.execute("DELETE FROM run_witnesses WHERE run_id=?", (result.run_id,))
            for i, w in enumerate(result.witnesses):
                self._conn.execute(
                    "INSERT INTO run_witnesses(run_id, ordinal, category, request_id, request_json,"
                    " old_verdict, new_verdict) VALUES(?,?,?,?,?,?,?)",
                    (
                        result.run_id, i, w.category.value, request_identity(w.request),
                        json.dumps(w.request, ensure_ascii=False),
                        w.old_verdict.value, w.new_verdict.value,
                    ),
                )
            self._conn.commit()

    def save_failure(self, run_id: str, old_version_id: str | None, new_version_id: str | None,
                     code: Failure, message: str, details: dict[str, Any], created_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, old_version_id, new_version_id, status,"
                " result_json, failure_code, failure_message, failure_details, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, old_version_id, new_version_id, "failed", None,
                 code.value, message, json.dumps(details, ensure_ascii=False), created_at),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, old_version_id, new_version_id, status, created_at"
                " FROM runs ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_witnesses(self, run_id: str, category: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM run_witnesses WHERE run_id=?"
        params: list[Any] = [run_id]
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY ordinal"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["request"] = json.loads(d.pop("request_json"))
            out.append(d)
        return out

    # ---- audit --------------------------------------------------------------
    def append_audit_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_events(run_id, request_id, stage, caller_location, detail_json,"
                " created_at, prev_hash, entry_hash, signature) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event.get("run_id"), event.get("request_id"), event["stage"],
                    event.get("caller_location"), json.dumps(event.get("detail", {}), ensure_ascii=False),
                    event["created_at"], event.get("prev_hash"), event.get("entry_hash"),
                    event.get("signature"),
                ),
            )
            self._conn.commit()

    def audit_events(self, run_id: str | None = None, request_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if request_id is not None:
            clauses.append("request_id=?")
            params.append(request_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    def latest_audit_hash(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT entry_hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        return row["entry_hash"] if row else None

    def iter_audit_rows(self) -> Iterable[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM audit_events ORDER BY seq"))
