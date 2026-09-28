"""Append-only audit store with an HMAC hash chain.

Every review appends one row. Row ``n`` carries
``chain_n = HMAC(key, chain_{n-1} || canonical(record_n))`` so that any
modification, deletion or reordering is detected by :meth:`verify_chain`.
The store keeps review evidence only — template text, verdict, finding codes
and *redacted* parameter descriptors. Bound values never enter the store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

GENESIS = b"sqlguard-genesis"
SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_records (
    seq INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL,
    verdict TEXT NOT NULL,
    statement_type TEXT,
    template_sha256 TEXT NOT NULL,
    finding_codes TEXT NOT NULL,
    evidence TEXT NOT NULL,
    chain TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_records(ts);
"""


def derive_key(secret: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=200_000)
    return kdf.derive(secret)


def load_or_create_key(key_path: str | Path, secret_env: str = "SQLGUARD_AUDIT_SECRET") -> bytes:
    """Load the audit HMAC key.

    Resolution order: ``$SQLGUARD_AUDIT_SECRET`` (dev/test convenience, derived
    with a fixed salt) or a local key file with a random PBKDF2 salt.
    Never ship a production secret in the repository.
    """
    kp = Path(key_path)
    env = os.environ.get(secret_env)
    if env:
        return derive_key(env.encode("utf-8"), b"sqlguard-env-salt")
    if kp.exists():
        return kp.read_bytes()
    kp.parent.mkdir(parents=True, exist_ok=True)
    import secrets as _secrets
    key = derive_key(_secrets.token_bytes(32), _secrets.token_bytes(16))
    kp.write_bytes(key)
    kp.chmod(0o600)
    return key


def _canonical(seq: int, request_id: str, ts: float, verdict: str,
               statement_type: str | None, template_sha256: str,
               finding_codes: list[str], evidence: dict[str, Any]) -> bytes:
    payload = [seq, request_id, ts, verdict, statement_type,
               template_sha256, finding_codes, evidence]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class ChainReport:
    ok: bool
    records: int
    first_bad_seq: int | None = None
    reason: str | None = None


class AuditStore:
    def __init__(self, db_path: str | Path, key: bytes):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = key
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AuditStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def new_request_id() -> str:
        return f"req_{uuid.uuid4().hex}"

    def append(self, *, request_id: str | None = None, template: str,
               result_dict: dict[str, Any]) -> str:
        request_id = request_id or self.new_request_id()
        ts = time.time()
        verdict = result_dict["verdict"]
        stype = result_dict.get("statement_type")
        codes = (
            result_dict["codes"]["reject"]
            + result_dict["codes"]["unanalyzable"]
            + result_dict["codes"]["advisory"]
        )
        template_sha = hashlib.sha256(template.encode("utf-8")).hexdigest()
        evidence = {
            "rendered_sql": result_dict.get("rendered_sql"),
            "resolved_identifiers": result_dict.get("resolved_identifiers", {}),
            "param_diagnostics": result_dict.get("param_diagnostics", []),
            "coverage": result_dict.get("coverage", {}),
            "findings": [
                {"code": f["code"], "severity": f["severity"],
                 "context": f.get("context"),
                 "location": f.get("location")}
                for f in result_dict.get("findings", [])
            ],
        }
        with self._lock:
            prev = self._conn.execute(
                "SELECT chain FROM audit_records ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_chain = bytes.fromhex(prev[0]) if prev else GENESIS
            seq_row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM audit_records").fetchone()
            seq = seq_row[0]
            canon = _canonical(seq, request_id, ts, verdict, stype,
                               template_sha, codes, evidence)
            chain = hmac.new(self.key, prev_chain + canon, hashlib.sha256).hexdigest()
            self._conn.execute(
                "INSERT INTO audit_records (seq, request_id, ts, verdict, "
                "statement_type, template_sha256, finding_codes, evidence, chain) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, request_id, ts, verdict, stype, template_sha,
                 json.dumps(codes), json.dumps(evidence, sort_keys=True), chain))
            self._conn.commit()
        return request_id

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT seq, request_id, ts, verdict, statement_type, "
            "template_sha256, finding_codes, evidence, chain "
            "FROM audit_records WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            return None
        return {
            "seq": row[0], "request_id": row[1], "ts": row[2],
            "verdict": row[3], "statement_type": row[4],
            "template_sha256": row[5],
            "finding_codes": json.loads(row[6]),
            "evidence": json.loads(row[7]),
            "chain": row[8],
        }

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, request_id, ts, verdict, statement_type FROM "
            "audit_records ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [
            {"seq": r[0], "request_id": r[1], "ts": r[2],
             "verdict": r[3], "statement_type": r[4]}
            for r in rows
        ]

    def verify_chain(self) -> ChainReport:
        rows = self._conn.execute(
            "SELECT seq, request_id, ts, verdict, statement_type, "
            "template_sha256, finding_codes, evidence, chain "
            "FROM audit_records ORDER BY seq ASC").fetchall()
        prev = GENESIS
        expected_seq = 1
        for r in rows:
            seq, rid, ts, verdict, stype, tsha, codes, evidence, chain = r
            if seq != expected_seq:
                return ChainReport(False, len(rows), seq,
                                   f"sequence gap: expected {expected_seq}, got {seq}")
            canon = _canonical(seq, rid, ts, verdict, stype, tsha,
                               json.loads(codes), json.loads(evidence))
            calc = hmac.new(self.key, prev + canon, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(calc, chain):
                return ChainReport(False, len(rows), seq, "HMAC mismatch (row altered)")
            prev = bytes.fromhex(chain)
            expected_seq += 1
        return ChainReport(True, len(rows))
