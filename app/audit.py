"""Audit interface: encrypted SQLite store + per-request/session linkage.

Schema
------
requests   one row per API request (correlation identity, rule version)
fragments  one row per redacted mapping; original is AES+HMAC sealed
events     append-only hash-chained operational log

The audit read/reveal endpoints require the shared audit key; reveal
additionally returns decrypted originals and is itself audited.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from .crypto import FragmentCipher, IntegrityError, chain_hash
from .kernel import MappingRecord, Span
from .rules import RuleSet

_GENESIS = "0" * 64


class AuditDenied(PermissionError):
    """Category ``audit_access_denied``."""


class NotFound(KeyError):
    """Category ``not_found``."""


class AuditStore:
    def __init__(self, db_path: str, cipher: FragmentCipher) -> None:
        self._db_path = db_path
        self._cipher = cipher
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id    TEXT PRIMARY KEY,
                    session_id    TEXT,
                    created_ts    REAL NOT NULL,
                    endpoint      TEXT NOT NULL,
                    rule_profile  TEXT,
                    rule_version  TEXT,
                    rule_fingerprint TEXT,
                    input_length  INTEGER,
                    output_length INTEGER,
                    matches       INTEGER,
                    uncertain     INTEGER
                );
                CREATE TABLE IF NOT EXISTS fragments (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id    TEXT NOT NULL REFERENCES requests(request_id),
                    session_id    TEXT,
                    rule_id       TEXT NOT NULL,
                    label         TEXT NOT NULL,
                    original_start INTEGER NOT NULL,
                    original_end  INTEGER NOT NULL,
                    output_start  INTEGER NOT NULL,
                    output_end    INTEGER NOT NULL,
                    original_length INTEGER NOT NULL,
                    replaced_length INTEGER NOT NULL,
                    uncertain     INTEGER NOT NULL,
                    reason        TEXT,
                    original_sha256 TEXT NOT NULL,
                    ciphertext    BLOB NOT NULL,
                    created_ts    REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id    TEXT,
                    session_id    TEXT,
                    event         TEXT NOT NULL,
                    payload       TEXT NOT NULL,
                    prev_hash     TEXT NOT NULL,
                    entry_hash    TEXT NOT NULL,
                    created_ts    REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_frag_req ON fragments(request_id);
                CREATE INDEX IF NOT EXISTS idx_frag_sess ON fragments(session_id);
                """
            )

    # -- requests ---------------------------------------------------------

    def record_request(
        self,
        *,
        request_id: str,
        session_id: Optional[str],
        endpoint: str,
        ruleset: Optional[RuleSet],
        input_length: int = 0,
        output_length: int = 0,
        matches: int = 0,
        uncertain: int = 0,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO requests(
                    request_id, session_id, created_ts, endpoint,
                    rule_profile, rule_version, rule_fingerprint,
                    input_length, output_length, matches, uncertain)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    session_id,
                    time.time(),
                    endpoint,
                    ruleset.profile if ruleset else None,
                    ruleset.version if ruleset else None,
                    ruleset.fingerprint if ruleset else None,
                    input_length,
                    output_length,
                    matches,
                    uncertain,
                ),
            )

    # -- fragments --------------------------------------------------------

    def record_fragments(
        self,
        *,
        request_id: str,
        session_id: Optional[str],
        mappings: Iterable[MappingRecord],
        originals_by_key: dict[tuple[int, int], str],
    ) -> int:
        rows = []
        for m in mappings:
            original = originals_by_key.get((m.original_start, m.original_end))
            if original is None:
                # Streaming sessions persist incrementally; spans that were
                # persisted in earlier calls are skipped by the caller.
                continue
            sealed = self._cipher.seal(original)
            rows.append(
                (
                    request_id,
                    session_id,
                    m.rule_id,
                    m.label,
                    m.original_start,
                    m.original_end,
                    m.output_start,
                    m.output_end,
                    m.original_length,
                    m.replaced_length,
                    1 if m.uncertain else 0,
                    m.reason,
                    m.original_sha256,
                    sealed.ciphertext,
                    time.time(),
                )
            )
        if not rows:
            return 0
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO fragments(
                    request_id, session_id, rule_id, label,
                    original_start, original_end, output_start, output_end,
                    original_length, replaced_length, uncertain, reason,
                    original_sha256, ciphertext, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    # -- events -----------------------------------------------------------

    def append_event(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        request_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        body_bytes = body.encode("utf-8")
        with self._lock:
            cur = self._conn.execute(
                "SELECT entry_hash FROM events ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            prev = row[0] if row else _GENESIS
            entry = chain_hash(prev, body_bytes)
            with self._conn:
                self._conn.execute(
                    """INSERT INTO events(
                        request_id, session_id, event, payload,
                        prev_hash, entry_hash, created_ts)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        request_id,
                        session_id,
                        event,
                        body,
                        prev,
                        entry,
                        time.time(),
                    ),
                )
        return entry

    def verify_chain(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload, prev_hash, entry_hash FROM events ORDER BY id"
            ).fetchall()
        prev = _GENESIS
        for row_id, payload, stored_prev, stored_entry in rows:
            if stored_prev != prev:
                return {"ok": False, "broken_at_event_id": row_id, "reason": "prev_mismatch"}
            body = payload.encode()
            if chain_hash(prev, body) != stored_entry:
                return {"ok": False, "broken_at_event_id": row_id, "reason": "entry_mismatch"}
            prev = stored_entry
        return {"ok": True, "events": len(rows), "tip": prev}

    # -- read side --------------------------------------------------------

    def list_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT request_id, session_id, created_ts, endpoint,
                          rule_profile, rule_version, rule_fingerprint,
                          input_length, output_length, matches, uncertain
                     FROM requests ORDER BY created_ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        keys = (
            "request_id", "session_id", "created_ts", "endpoint",
            "rule_profile", "rule_version", "rule_fingerprint",
            "input_length", "output_length", "matches", "uncertain",
        )
        return [dict(zip(keys, r)) for r in rows]

    def get_request(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT request_id, session_id, created_ts, endpoint, rule_profile,"
                " rule_version, rule_fingerprint, input_length, output_length,"
                " matches, uncertain FROM requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise NotFound(request_id)
        keys = (
            "request_id", "session_id", "created_ts", "endpoint",
            "rule_profile", "rule_version", "rule_fingerprint",
            "input_length", "output_length", "matches", "uncertain",
        )
        return dict(zip(keys, row))

    def list_fragments(
        self, request_id: str, *, reveal: bool, audit_key: str, expected_key: str
    ) -> list[dict[str, Any]]:
        self._authorize(audit_key, expected_key)
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, rule_id, label, original_start, original_end,
                          output_start, output_end, original_length,
                          replaced_length, uncertain, reason, original_sha256,
                          ciphertext
                     FROM fragments WHERE request_id=? ORDER BY id""",
                (request_id,),
            ).fetchall()
        out = []
        for (
            frag_id, rule_id, label, os_, oe, qs, qe, olen, rlen,
            uncertain, reason, digest, ciphertext,
        ) in rows:
            rec: dict[str, Any] = {
                "fragment_id": frag_id,
                "rule_id": rule_id,
                "label": label,
                "original_start": os_,
                "original_end": oe,
                "output_start": qs,
                "output_end": qe,
                "original_length": olen,
                "replaced_length": rlen,
                "uncertain": bool(uncertain),
                "reason": reason,
                "original_sha256": digest,
            }
            if reveal:
                rec["original"] = self._cipher.open_if_matches(ciphertext, digest)
            out.append(rec)
        return out

    def _authorize(self, provided: str, expected: str) -> None:
        import hmac

        if not hmac.compare_digest(provided or "", expected):
            raise AuditDenied("invalid audit key")

    # -- helpers for the service layer ------------------------------------

    def uncertain_payload(self, uncertain: list[Span]) -> list[dict[str, Any]]:
        """Shape uncertainty findings for event payloads (no originals)."""
        return [
            {
                "rule_id": u.rule_id,
                "label": u.label,
                "start": u.start,
                "end": u.end,
                "length": u.length,
                "reason": u.reason,
                "original_sha256": hashlib.sha256(u.original.encode()).hexdigest(),
            }
            for u in uncertain
        ]
