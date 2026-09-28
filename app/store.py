"""SQLite-backed state with per-run isolation.

Every persisted row is scoped by ``run_id``: there is no cross-run read
path. The run lifecycle is explicit:

    created  -> finalized

Writes to a finalized run raise ``StateConflictError``; looking up a run
that never existed raises ``NotFoundError``.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import NotFoundError, StateConflictError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('created', 'finalized')),
    policy_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    report_json TEXT,
    signature   TEXT,
    key_id      TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    run_id  TEXT NOT NULL,
    req_id  TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, req_id)
);
CREATE TABLE IF NOT EXISTS events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class SQLiteStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    def create_run(self, run_id: str, policy: dict[str, Any], created_at: str) -> None:
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO runs (run_id, status, policy_json, created_at) "
                    "VALUES (?, 'created', ?, ?)",
                    (run_id, json.dumps(policy, sort_keys=True), created_at),
                )
        except sqlite3.IntegrityError:
            raise StateConflictError(
                f"run {run_id!r} already exists",
                code="run.duplicate",
                detail={"run_id": run_id},
            )

    def finalize_run(
        self,
        run_id: str,
        report: dict[str, Any],
        signature: str,
        key_id: str,
    ) -> None:
        with self._tx() as conn:
            status = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if status is None:
                raise NotFoundError(
                    f"run {run_id!r} does not exist",
                    code="run.not_found",
                    detail={"run_id": run_id},
                )
            if status["status"] == "finalized":
                raise StateConflictError(
                    f"run {run_id!r} is already finalized",
                    code="run.finalized",
                    detail={"run_id": run_id},
                )
            conn.execute(
                "UPDATE runs SET status = 'finalized', report_json = ?, "
                "signature = ?, key_id = ? WHERE run_id = ?",
                (json.dumps(report, sort_keys=True), signature, key_id, run_id),
            )

    def save_request(self, run_id: str, req_id: str, payload: dict[str, Any]) -> None:
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO requests (run_id, req_id, payload) VALUES (?, ?, ?)",
                    (run_id, req_id, json.dumps(payload, sort_keys=True)),
                )
        except sqlite3.IntegrityError:
            raise StateConflictError(
                f"request {req_id!r} already stored in run {run_id!r}",
                code="request.duplicate",
                detail={"run_id": run_id, "request": req_id},
            )

    def save_events(self, run_id: str, events: list[dict[str, Any]]) -> None:
        # Events are append-only; writing after finalize is rejected here.
        status = self._conn.execute(
            "SELECT status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if status is None:
            raise NotFoundError(
                f"run {run_id!r} does not exist",
                code="run.not_found",
                detail={"run_id": run_id},
            )
        if status["status"] != "created":
            raise StateConflictError(
                f"run {run_id!r} is finalized; no more events",
                code="run.finalized",
                detail={"run_id": run_id},
            )
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO events (run_id, seq, payload) VALUES (?, ?, ?)",
                [(run_id, e["seq"], json.dumps(e, sort_keys=True)) for e in events],
            )

    # ------------------------------------------------------------------
    def get_report(self, run_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT report_json, signature FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"run {run_id!r} does not exist",
                code="run.not_found",
                detail={"run_id": run_id},
            )
        if row["report_json"] is None:
            raise StateConflictError(
                f"run {run_id!r} has not been finalized yet",
                code="run.not_finalized",
                detail={"run_id": run_id},
            )
        return json.loads(row["report_json"])

    def get_events(self, run_id: str) -> list[dict[str, Any]]:
        exists = self._conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if exists is None:
            raise NotFoundError(
                f"run {run_id!r} does not exist",
                code="run.not_found",
                detail={"run_id": run_id},
            )
        rows = self._conn.execute(
            "SELECT payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_signature_row(self, run_id: str) -> tuple[dict[str, Any], str, str]:
        row = self._conn.execute(
            "SELECT report_json, signature, key_id FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(
                f"run {run_id!r} does not exist",
                code="run.not_found",
                detail={"run_id": run_id},
            )
        if row["report_json"] is None:
            raise StateConflictError(
                f"run {run_id!r} has not been finalized yet",
                code="run.not_finalized",
                detail={"run_id": run_id},
            )
        return json.loads(row["report_json"]), row["signature"], row["key_id"]
