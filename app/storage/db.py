"""SQLite persistence — the state-isolation layer.

One database file belongs to exactly one running configuration. Tests point
``AUDIT_DB_PATH`` at an isolated temp file so state never leaks between runs.
The connection never lives in import-time globals; the FastAPI app owns one
``Database`` instance and exposes it through dependency injection (overridable
in tests).

Stored material classification (see docs/DESIGN.md §3):
  * PUBLIC  — batch id, schema (names/types), commitments, root, leaf index;
  * PRIVATE — salts and raw values. They stay in the local DB and are only
              emitted for explicitly requested disclosure fields.

The audit table is append-only: every state-changing or verification action
writes one row with the run/request identity and the concrete verdict code,
never a blanket "success".
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id      TEXT PRIMARY KEY,
    schema_json   TEXT NOT NULL,
    record_count  INTEGER NOT NULL,
    root_hex      TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    closed        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS leaves (
    batch_id        TEXT NOT NULL,
    leaf_index      INTEGER NOT NULL,
    record_index    INTEGER NOT NULL,
    field_position  INTEGER NOT NULL,
    field_name      TEXT NOT NULL,
    field_type      TEXT NOT NULL,
    salted          INTEGER NOT NULL,
    salt_hex        TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    encoded_hex     TEXT NOT NULL,
    commitment_hex  TEXT NOT NULL,
    PRIMARY KEY (batch_id, record_index, field_position),
    UNIQUE (batch_id, leaf_index),
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    batch_id   TEXT,
    action     TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leaves_batch ON leaves(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_log(batch_id, seq);
"""


@dataclass(frozen=True)
class BatchRow:
    batch_id: str
    schema: list[dict]
    record_count: int
    root_hex: str
    warnings: list[str]
    created_at: str
    closed: bool


@dataclass(frozen=True)
class LeafRow:
    leaf_index: int
    record_index: int
    field_position: int
    field_name: str
    field_type: str
    salted: bool
    salt_hex: str
    value: Any
    encoded_hex: str
    commitment_hex: str


@dataclass(frozen=True)
class AuditRow:
    seq: int
    ts: str
    run_id: str
    batch_id: str | None
    action: str
    verdict: str
    detail: dict


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    # ------------------------------------------------------------------ basic
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- batches
    def insert_batch(
        self,
        batch_id: str,
        schema: list[dict],
        root_hex: str,
        leaves: Iterable[dict],
        warnings: list[str],
        created_at: str | None = None,
    ) -> None:
        leaves = list(leaves)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO batches(batch_id, schema_json, record_count, root_hex, "
                    "warnings_json, created_at) VALUES (?,?,?,?,?,?)",
                    (
                        batch_id,
                        json.dumps(schema, ensure_ascii=False, sort_keys=True),
                        len({d["record_index"] for d in leaves}),
                        root_hex,
                        json.dumps(warnings, ensure_ascii=False),
                        created_at or utc_now_iso(),
                    ),
                )
                self._conn.executemany(
                    "INSERT INTO leaves(batch_id, leaf_index, record_index, field_position, "
                    "field_name, field_type, salted, salt_hex, value_json, encoded_hex, "
                    "commitment_hex) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            batch_id,
                            d["leaf_index"],
                            d["record_index"],
                            d["field_position"],
                            d["field_name"],
                            d["field_type"],
                            1 if d["salted"] else 0,
                            d["salt_hex"],
                            json.dumps(d["value"], ensure_ascii=False, sort_keys=True),
                            d["encoded_hex"],
                            d["commitment_hex"],
                        )
                        for d in leaves
                    ],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_batch(self, batch_id: str) -> BatchRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            return None
        return BatchRow(
            batch_id=row["batch_id"],
            schema=json.loads(row["schema_json"]),
            record_count=row["record_count"],
            root_hex=row["root_hex"],
            warnings=json.loads(row["warnings_json"]),
            created_at=row["created_at"],
            closed=bool(row["closed"]),
        )

    def list_leaves(self, batch_id: str) -> list[LeafRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM leaves WHERE batch_id = ? ORDER BY leaf_index", (batch_id,)
            ).fetchall()
        return [self._leaf_row(r) for r in rows]

    def get_leaf(self, batch_id: str, record_index: int, field_position: int) -> LeafRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leaves WHERE batch_id = ? AND record_index = ? AND field_position = ?",
                (batch_id, record_index, field_position),
            ).fetchone()
        return self._leaf_row(row) if row is not None else None

    @staticmethod
    def _leaf_row(row: sqlite3.Row) -> LeafRow:
        return LeafRow(
            leaf_index=row["leaf_index"],
            record_index=row["record_index"],
            field_position=row["field_position"],
            field_name=row["field_name"],
            field_type=row["field_type"],
            salted=bool(row["salted"]),
            salt_hex=row["salt_hex"],
            value=json.loads(row["value_json"]),
            encoded_hex=row["encoded_hex"],
            commitment_hex=row["commitment_hex"],
        )

    # ------------------------------------------------------------------ audit
    def append_audit(
        self,
        *,
        run_id: str,
        action: str,
        verdict: str,
        batch_id: str | None = None,
        detail: dict | None = None,
    ) -> AuditRow:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit_log(ts, run_id, batch_id, action, verdict, detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    utc_now_iso(),
                    run_id,
                    batch_id,
                    action,
                    verdict,
                    json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            self._conn.commit()
            seq = cur.lastrowid
            row = self._conn.execute("SELECT * FROM audit_log WHERE seq = ?", (seq,)).fetchone()
        return AuditRow(
            seq=row["seq"],
            ts=row["ts"],
            run_id=row["run_id"],
            batch_id=row["batch_id"],
            action=row["action"],
            verdict=row["verdict"],
            detail=json.loads(row["detail_json"]),
        )

    def list_audit(self, batch_id: str | None = None, limit: int = 100) -> list[AuditRow]:
        with self._lock:
            if batch_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log WHERE batch_id = ? ORDER BY seq DESC LIMIT ?",
                    (batch_id, limit),
                ).fetchall()
        return [
            AuditRow(
                seq=r["seq"],
                ts=r["ts"],
                run_id=r["run_id"],
                batch_id=r["batch_id"],
                action=r["action"],
                verdict=r["verdict"],
                detail=json.loads(r["detail_json"]),
            )
            for r in rows
        ]
