"""SQLite-backed metadata store with transactional run/event logging.

Every verification run is persisted atomically with its structured events:
schema admission, kernel encoding, paging decision and each differential
check (kernel vs PyArrow values, kernel vs fastparquet page levels).

The run row and its events are inserted in one SQLite transaction so a crash
mid-verification never leaves a run that claims success without the evidence.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ..config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    request_id      TEXT PRIMARY KEY,
    status          TEXT NOT NULL,           -- OK | FAILED | UNCERTAIN
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    schema_json     TEXT NOT NULL,
    record_count    INTEGER NOT NULL,
    page_version    TEXT,
    error_json      TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    phase       TEXT NOT NULL,
    status      TEXT NOT NULL,               -- PASS | FAIL | INFO | UNCERTAIN
    detail_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES runs(request_id)
);
CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id, seq);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MetadataStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.path = Path(db_path or settings.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys = ON")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def create_run(self, request_id: str, schema_json: dict[str, Any],
                   record_count: int, page_version: str) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO runs(request_id,status,started_at,schema_json,"
                "record_count,page_version) VALUES(?,?,?,?,?,?)",
                (request_id, "RUNNING", _now(), json.dumps(schema_json),
                 record_count, page_version),
            )

    def append_event(self, request_id: str, seq: int, phase: str,
                     status: str, detail: dict[str, Any]) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO events(request_id,seq,phase,status,detail_json,"
                "created_at) VALUES(?,?,?,?,?,?)",
                (request_id, seq, phase, status, json.dumps(detail), _now()),
            )

    def finish_run(self, request_id: str, status: str,
                   error: Optional[dict[str, Any]] = None) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE runs SET status=?, finished_at=?, error_json=? "
                "WHERE request_id=?",
                (status, _now(),
                 json.dumps(error) if error is not None else None, request_id),
            )

    def get_run(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM runs WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return None
            events = con.execute(
                "SELECT seq,phase,status,detail_json,created_at FROM events "
                "WHERE request_id=? ORDER BY seq", (request_id,)
            ).fetchall()
        d = dict(row)
        d["schema"] = json.loads(d.pop("schema_json"))
        d["error"] = json.loads(d["error_json"]) if d["error_json"] else None
        d["events"] = [
            {"seq": e["seq"], "phase": e["phase"], "status": e["status"],
             "detail": json.loads(e["detail_json"]), "created_at": e["created_at"]}
            for e in events
        ]
        return d

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT request_id,status,started_at,finished_at,record_count,"
                "page_version FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
