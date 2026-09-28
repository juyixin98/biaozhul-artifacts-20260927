"""SQLite persistence with explicit state isolation.

Three tables keep three trust levels apart:

* ``batches``       - public batch metadata and the batch root;
* ``batch_secrets`` - private per-field salts/values, never selected by any
                      public/listing code path;
* ``audit_events``  - append-only audit trail; detail JSON is whitelisted by
                      the audit layer and never contains raw secrets.

Access goes through short-lived connections (``check_same_thread=False`` with
an explicit lock is unnecessary for this workload because every write is a
single autocommit transaction on one local file).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.core.errors import BatchNotFound, CoreError

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id      TEXT PRIMARY KEY,
    digest        TEXT NOT NULL,
    batch_root    TEXT NOT NULL,
    field_count   INTEGER NOT NULL,
    record_count  INTEGER NOT NULL,
    public_json   TEXT NOT NULL,
    created_run   TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS batch_secrets (
    batch_id      TEXT NOT NULL,
    record_index  INTEGER NOT NULL,
    path          TEXT NOT NULL,
    state         TEXT NOT NULL,
    salt_hex      TEXT,
    value_json    TEXT,
    PRIMARY KEY (batch_id, record_index, path),
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    batch_id    TEXT,
    verdict     TEXT,
    category    TEXT,
    fingerprint TEXT,
    detail_json TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_events(batch_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # -- public / private separation is enforced by method surface ---------

    def insert_batch(
        self,
        *,
        batch_id: str,
        digest: str,
        batch_root: str,
        field_count: int,
        record_count: int,
        public_json: str,
        secrets: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        with self.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO batches(batch_id, digest, batch_root, "
                    "field_count, record_count, public_json, created_run) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        batch_id,
                        digest,
                        batch_root,
                        field_count,
                        record_count,
                        public_json,
                        run_id,
                    ),
                )
            except sqlite3.IntegrityError:
                raise CoreError(
                    f"batch {batch_id!r} already exists",
                )
            conn.executemany(
                "INSERT INTO batch_secrets(batch_id, record_index, path, "
                "state, salt_hex, value_json) VALUES (?,?,?,?,?,?)",
                [
                    (
                        batch_id,
                        s["record_index"],
                        s["path"],
                        s["state"],
                        s.get("salt_hex"),
                        json.dumps(s.get("value"), ensure_ascii=False)
                        if "value" in s
                        else None,
                    )
                    for s in secrets
                ],
            )

    def public_batch(self, batch_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT public_json FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            raise BatchNotFound(f"no batch with id {batch_id!r}")
        return json.loads(row["public_json"])

    def batch_root(self, batch_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT batch_root FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            raise BatchNotFound(f"no batch with id {batch_id!r}")
        return row["batch_root"]

    def list_public_batches(self) -> list[dict[str, Any]]:
        """Listing intentionally touches only the public table."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT batch_id, digest, batch_root, field_count, "
                "record_count, created_at FROM batches ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def secret_cell(self, batch_id: str, record_index: int, path: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state, salt_hex, value_json FROM batch_secrets "
                "WHERE batch_id=? AND record_index=? AND path=?",
                (batch_id, record_index, path),
            ).fetchone()
        if row is None:
            # Distinguish unknown batch from unknown cell for precise errors.
            self.batch_root(batch_id)
            from app.core.errors import FieldNotCommitted

            raise FieldNotCommitted(
                f"no committed cell for {path!r} in record {record_index}"
            )
        return {
            "state": row["state"],
            "salt_hex": row["salt_hex"],
            "value": json.loads(row["value_json"]) if row["value_json"] is not None else None,
        }

    def insert_audit_event(self, event: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_events(run_id, event_type, batch_id, "
                "verdict, category, fingerprint, detail_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    event["run_id"],
                    event["event_type"],
                    event.get("batch_id"),
                    event.get("verdict"),
                    event.get("category"),
                    event.get("fingerprint"),
                    json.dumps(event.get("detail", {}), ensure_ascii=False),
                ),
            )

    def audit_events(
        self, *, run_id: str | None = None, batch_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: list[Any] = []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if batch_id is not None:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        sql += " ORDER BY id"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
