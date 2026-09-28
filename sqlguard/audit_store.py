"""Encrypted audit-record store.

Each review is persisted as one row. The record body is Fernet-encrypted and
the request id is stored only as an HMAC lookup token, so the on-disk
database discloses neither request ids nor review content.

This store is intentionally separate from the read-only fixture database
(see :mod:`sqlguard.isolation`): reviews write here, user SQL is never
executed anywhere.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .crypto import KeyMaterial

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_index TEXT NOT NULL UNIQUE,
    verdict TEXT NOT NULL,
    created_at REAL NOT NULL,
    ciphertext BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_verdict ON audit_records(verdict, created_at);
"""


class AuditStore:
    def __init__(self, db_path: str | Path, key: KeyMaterial) -> None:
        self.path = str(Path(db_path))
        self.key = key
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, result_dict: dict) -> dict:
        rid = result_dict["request_id"]
        token = self.key.index_token(rid)
        body = json.dumps(result_dict, sort_keys=True,
                          ensure_ascii=False).encode("utf-8")
        ciphertext = self.key.encrypt(body)
        self._conn.execute(
            "INSERT OR REPLACE INTO audit_records "
            "(request_index, verdict, created_at, ciphertext) "
            "VALUES (?, ?, ?, ?)",
            (token, result_dict["verdict"], time.time(), ciphertext),
        )
        self._conn.commit()
        return {"request_id": rid, "stored": True,
                "record_index": token[:12]}

    def fetch(self, request_id: str) -> dict | None:
        token = self.key.index_token(request_id)
        cur = self._conn.execute(
            "SELECT ciphertext FROM audit_records WHERE request_index = ?",
            (token,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return json.loads(self.key.decrypt(row[0]))

    def recent_verdicts(self, limit: int = 20) -> list[dict]:
        """Return verdict-only metadata (no decryption) for dashboards."""
        cur = self._conn.execute(
            "SELECT request_index, verdict, created_at FROM audit_records "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {"record_index": idx[:12], "verdict": v, "created_at": ts}
            for idx, v, ts in cur.fetchall()
        ]

    def close(self) -> None:
        self._conn.close()
