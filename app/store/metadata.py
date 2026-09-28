"""SQLite metadata store with explicit transactions.

Three tables:

* ``columns``        - one row per imported column/derived view currently held
* ``operations``     - append-only journal of import/slice/concat/validate ops
* ``validations``    - the structured validation reports attached to operations

``record_operation`` runs the caller's work + journal insert in one
``BEGIN IMMEDIATE`` transaction: if the kernel raises, the metadata rolls back
and nothing is half-recorded. A separate :meth:`record_audit` commits audit rows
independently (a failed validation must still be auditable).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from app.logging_setup import get_logger

LOG = get_logger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS columns (
    column_id     TEXT PRIMARY KEY,
    type_name     TEXT NOT NULL,
    logical_offset INTEGER NOT NULL,
    logical_length INTEGER NOT NULL,
    null_count    INTEGER NOT NULL,
    source        TEXT NOT NULL,
    parent_ids    TEXT NOT NULL,
    buffers_json  TEXT NOT NULL,
    created_run_id TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    op_id        TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    op_type      TEXT NOT NULL,
    status       TEXT NOT NULL,
    input_fp     TEXT,
    result_id    TEXT,
    detail_json  TEXT NOT NULL,
    error_json   TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validations (
    op_id        TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    report_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (op_id)
);
"""

T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MetadataStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit off is managed explicitly via BEGIN below; isolation_level
        # None puts sqlite3 in autocommit mode so our BEGIN is authoritative.
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._txn_depth = 0
        with self.transaction() as conn:
            conn.executescript(SCHEMA)
        LOG.info("metadata store opened at %s", self.db_path)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant explicit transaction.

        The outer level opens ``BEGIN IMMEDIATE``; nested levels use SAVEPOINTs
        so a failure inside ``work`` (which may itself call store methods)
        rolls back the whole unit, while inner callers stay composable.
        """
        if self._txn_depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
            self._txn_depth = 1
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                self._txn_depth = 0
                raise
            else:
                self.conn.commit()
                self._txn_depth = 0
            return

        savepoint = f"sp_{self._txn_depth}"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        self._txn_depth += 1
        try:
            yield self.conn
        except Exception:
            self.conn.execute(f"ROLLBACK TO {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            self._txn_depth -= 1
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            self._txn_depth -= 1

    # ------------------------------------------------------------- columns
    def upsert_column(self, *, column_id: str, type_name: str, logical_offset: int,
                      logical_length: int, null_count: int, source: str,
                      parent_ids: list[str], buffers: list[dict], run_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO columns(column_id, type_name, logical_offset, logical_length,
                       null_count, source, parent_ids, buffers_json, created_run_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(column_id) DO UPDATE SET
                       type_name=excluded.type_name,
                       logical_offset=excluded.logical_offset,
                       logical_length=excluded.logical_length,
                       null_count=excluded.null_count,
                       buffers_json=excluded.buffers_json""",
                (column_id, type_name, logical_offset, logical_length, null_count, source,
                 json.dumps(parent_ids), json.dumps(buffers), run_id, _now()),
            )

    def get_column(self, column_id: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM columns WHERE column_id=?", (column_id,))
        return cur.fetchone()

    def list_columns(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT column_id, type_name, logical_offset, logical_length, null_count, source "
            "FROM columns ORDER BY created_at"))

    def delete_column(self, column_id: str) -> bool:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM columns WHERE column_id=?", (column_id,))
            return cur.rowcount > 0

    # ---------------------------------------------------------- operations
    def record_operation(self, *, op_id: str, run_id: str, op_type: str, status: str,
                         input_fp: str | None, result_id: str | None,
                         detail: dict, error: dict | None,
                         work: Callable[[], T] | None = None) -> T | None:
        """Execute ``work`` and journal the operation atomically.

        If ``work`` raises, the whole transaction rolls back and the exception
        propagates; the caller decides whether to also :meth:`record_audit` the
        failure independently.
        """
        result: T | None = None
        with self.transaction() as conn:
            if work is not None:
                result = work()
            conn.execute(
                """INSERT INTO operations(op_id, run_id, op_type, status, input_fp, result_id,
                       detail_json, error_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (op_id, run_id, op_type, status, input_fp, result_id,
                 json.dumps(detail), json.dumps(error) if error else None, _now()),
            )
        LOG.info("operation %s recorded: %s status=%s result=%s",
                 op_id, op_type, status, result_id)
        return result

    def record_audit(self, *, op_id: str, run_id: str, op_type: str, status: str,
                     input_fp: str | None, detail: dict, error: dict | None) -> None:
        """Independent audit write (commits even when the business op failed)."""
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO operations(op_id, run_id, op_type, status, input_fp, result_id,
                       detail_json, error_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (op_id, run_id, op_type, status, input_fp, None,
                 json.dumps(detail), json.dumps(error) if error else None, _now()),
            )

    def attach_validation(self, op_id: str, report: dict) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO validations(op_id, ok, report_json, created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(op_id) DO UPDATE SET ok=excluded.ok, report_json=excluded.report_json""",
                (op_id, 1 if report["ok"] else 0, json.dumps(report), _now()),
            )

    def get_operation(self, op_id: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,))
        return cur.fetchone()
