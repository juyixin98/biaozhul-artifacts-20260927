"""Audit interface — append-only SQLite log with an HMAC hash chain.

Every run and every significant processing step (stage + outcome + the
criterion it was decided on) is persisted. Events are linked by an
HMAC-SHA256 chain so that tampering with a single row is detectable via
:meth:`AuditDB.verify_chain`.

The HMAC key is a local secret generated on first use (``audit_key.bin`` next
to the database, mode 0600) — no external KMS or production account needed.
HMAC is provided by the ``cryptography`` package.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hmac as crypto_hmac
from cryptography.hazmat.primitives import hashes

GENESIS_HASH = "0" * 64
KEY_FILE_NAME = "audit_key.bin"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class AuditDB:
    def __init__(self, db_path: str | Path, *, version: str):
        self.db_path = Path(db_path)
        self.version = version
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create_key()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- key management -----------------------------------------------------
    def _load_or_create_key(self) -> bytes:
        key_path = self.db_path.parent / KEY_FILE_NAME
        if key_path.exists():
            key = key_path.read_bytes()
            if len(key) >= 32:
                return key
        key = os.urandom(32)
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key

    def _hmac(self, payload: dict[str, Any]) -> str:
        h = crypto_hmac.HMAC(self._key, hashes.SHA256())
        h.update(_canonical(payload))
        return h.finalize().hex()

    # -- schema -------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id      TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL,
                    finalized_at TEXT,
                    verdict     TEXT NOT NULL,
                    category    TEXT,
                    container   TEXT,
                    filename    TEXT,
                    input_sha256 TEXT,
                    input_size  INTEGER,
                    summary     TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    ts          TEXT NOT NULL,
                    version     TEXT NOT NULL,
                    stage       TEXT NOT NULL,
                    outcome     TEXT NOT NULL,
                    category    TEXT,
                    evidence    TEXT,
                    message     TEXT,
                    detail      TEXT,
                    prev_hash   TEXT NOT NULL,
                    event_hash  TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -------------------------------------------------------------
    def create_run(
        self,
        run_id: str,
        *,
        filename: str,
        container: str | None,
        input_sha256: str,
        input_size: int,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, created_at, verdict, container, filename,
                    input_sha256, input_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    utc_now_iso(),
                    "started",
                    container,
                    filename,
                    input_sha256,
                    input_size,
                ),
            )

    def set_container(self, run_id: str, container: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET container=? WHERE run_id=?", (container, run_id)
            )

    def record_event(
        self,
        run_id: str,
        stage: str,
        outcome: str,
        *,
        category: str | None = None,
        evidence: str | None = None,
        message: str | None = None,
        detail: dict | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT event_hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            prev_hash = row["event_hash"] if row else GENESIS_HASH
            ts = utc_now_iso()
            # seq is autoincrement; select it after insert.
            cur = self._conn.execute(
                """INSERT INTO events
                   (run_id, ts, version, stage, outcome, category, evidence,
                    message, detail, prev_hash, event_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, ts, self.version, stage, outcome, category, evidence,
                    message,
                    json.dumps(detail, sort_keys=True, ensure_ascii=False) if detail else None,
                    prev_hash,
                    "",  # filled below once seq known
                ),
            )
            seq = cur.lastrowid
            payload = {
                "seq": seq,
                "run_id": run_id,
                "ts": ts,
                "version": self.version,
                "stage": stage,
                "outcome": outcome,
                "category": category,
                "evidence": evidence,
                "message": message,
                "detail": detail,
                "prev_hash": prev_hash,
            }
            event_hash = self._hmac(payload)
            self._conn.execute(
                "UPDATE events SET event_hash=? WHERE seq=?", (event_hash, seq)
            )
            self._conn.commit()
        payload["event_hash"] = event_hash
        return payload

    def finalize_run(
        self,
        run_id: str,
        *,
        verdict: str,
        category: str | None,
        summary: dict | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE runs SET finalized_at=?, verdict=?, category=?, summary=?
                   WHERE run_id=?""",
                (
                    utc_now_iso(),
                    verdict,
                    category,
                    json.dumps(summary, sort_keys=True, ensure_ascii=False) if summary else None,
                    run_id,
                ),
            )

    # -- reads --------------------------------------------------------------
    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        if data.get("summary"):
            data["summary"] = json.loads(data["summary"])
        return data

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, created_at, finalized_at, verdict, category, "
                "container, filename, input_sha256, input_size "
                "FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_events(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq ASC", (run_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("detail"):
                d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    # -- integrity verification --------------------------------------------
    def verify_chain(self, run_id: str | None = None) -> dict[str, Any]:
        """Recompute every event_hash and check links. Returns a report."""
        with self._lock:
            if run_id:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE run_id=? ORDER BY seq ASC", (run_id,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY seq ASC"
                ).fetchall()

        expected_prev: dict[str, str] = {}
        checked = 0
        for r in rows:
            d = dict(r)
            prev = expected_prev.get(d["run_id"], GENESIS_HASH)
            if d["prev_hash"] != prev:
                return {
                    "ok": False,
                    "checked": checked,
                    "broken_at": d["seq"],
                    "run_id": d["run_id"],
                    "reason": "prev_hash link mismatch",
                }
            payload = {
                "seq": d["seq"],
                "run_id": d["run_id"],
                "ts": d["ts"],
                "version": d["version"],
                "stage": d["stage"],
                "outcome": d["outcome"],
                "category": d["category"],
                "evidence": d["evidence"],
                "message": d["message"],
                "detail": json.loads(d["detail"]) if d["detail"] else None,
                "prev_hash": d["prev_hash"],
            }
            actual = self._hmac(payload)
            if not _constant_time_eq(actual, d["event_hash"]):
                return {
                    "ok": False,
                    "checked": checked,
                    "broken_at": d["seq"],
                    "run_id": d["run_id"],
                    "reason": "event_hash HMAC mismatch",
                }
            expected_prev[d["run_id"]] = d["event_hash"]
            checked += 1
        return {"ok": True, "checked": checked, "broken_at": None, "run_id": run_id}


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac as stdlib_hmac

    return stdlib_hmac.compare_digest(a, b)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
