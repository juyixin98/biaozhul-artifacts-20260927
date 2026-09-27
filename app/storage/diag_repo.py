"""Diagnostic event repository."""
from __future__ import annotations

from typing import List, Optional

from .db import utc_now


class DiagnosticRepo:
    def __init__(self, db):
        self._db = db

    def insert(
        self,
        *,
        request_id: Optional[str],
        kind: str,
        summary: str,
        method: Optional[str] = None,
        path: Optional[str] = None,
        decision: Optional[str] = None,
        code: Optional[str] = None,
        state_json: Optional[str] = None,
    ) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO diagnostic_events(request_id, kind, method, path, "
                "decision, code, summary, state_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (request_id, kind, method, path, decision, code, summary,
                 state_json, utc_now()),
            )
            return int(cur.lastrowid)

    def get_for_request(self, request_id: str) -> List:
        return self._db.execute(
            "SELECT * FROM diagnostic_events WHERE request_id=? "
            "ORDER BY id ASC",
            (request_id,),
        ).fetchall()

    def list_recent(self, limit: int = 100) -> List:
        return self._db.execute(
            "SELECT * FROM diagnostic_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
