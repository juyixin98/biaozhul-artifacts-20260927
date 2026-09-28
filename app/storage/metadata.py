"""SQLite-backed metadata store.

Each validation request is recorded atomically: the request row, its per-check
steps and artifacts commit together, or not at all. Results remain queryable by
request identity after the fact.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..config import Settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS requests (
    request_id      TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    schema_json     TEXT NOT NULL,
    record_count    INTEGER NOT NULL,
    artifact_path   TEXT,
    page_count      INTEGER,
    error_category  TEXT,
    error_detail    TEXT,
    warnings_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL REFERENCES requests(request_id),
    step_index      INTEGER NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,
    detail          TEXT,
    UNIQUE(request_id, step_index)
);

CREATE TABLE IF NOT EXISTS artifacts (
    request_id      TEXT PRIMARY KEY REFERENCES requests(request_id),
    self_path       TEXT NOT NULL,
    oracle_path     TEXT,
    self_size       INTEGER NOT NULL,
    oracle_size     INTEGER
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MetadataStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_request(self, request_id: str, schema_json: dict,
                       record_count: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO requests(request_id, created_at, status, "
                "schema_json, record_count, warnings_json) VALUES (?,?,?,?,?,?)",
                (request_id, _now(), "running",
                 json.dumps(schema_json, ensure_ascii=False), record_count, "[]"))

    def add_step(self, request_id: str, index: int, name: str, status: str,
                 detail: dict | str | None = None) -> None:
        detail_text = (
            detail if isinstance(detail, str)
            else json.dumps(detail, ensure_ascii=False, default=str)
            if detail is not None else None)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO steps(request_id, step_index, name, status, detail)"
                " VALUES (?,?,?,?,?)",
                (request_id, index, name, status, detail_text))

    def complete_request(self, request_id: str, status: str,
                         artifact_path: str | None, page_count: int | None,
                         warnings: list[Any],
                         error_category: str | None = None,
                         error_detail: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE requests SET status=?, artifact_path=?, page_count=?,"
                " error_category=?, error_detail=?, warnings_json=? "
                "WHERE request_id=?",
                (status, artifact_path, page_count, error_category, error_detail,
                 json.dumps(warnings, ensure_ascii=False, default=str),
                 request_id))

    def attach_artifacts(self, request_id: str, self_path: str,
                         oracle_path: str | None, self_size: int,
                         oracle_size: int | None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO artifacts(request_id, self_path, oracle_path,"
                " self_size, oracle_size) VALUES (?,?,?,?,?)",
                (request_id, self_path, oracle_path, self_size, oracle_size))

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id=?",
                (request_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["steps"] = [dict(r) for r in conn.execute(
                "SELECT step_index, name, status, detail FROM steps "
                "WHERE request_id=? ORDER BY step_index", (request_id,))]
            art = conn.execute(
                "SELECT * FROM artifacts WHERE request_id=?",
                (request_id,)).fetchone()
            result["artifact"] = dict(art) if art else None
            result["warnings"] = json.loads(result["warnings_json"])
            return result

    def list_requests(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT request_id, created_at, status, record_count,"
                " error_category FROM requests ORDER BY created_at DESC "
                "LIMIT ?", (limit,))]


def get_store(settings: Settings) -> MetadataStore:
    return MetadataStore(settings.db_path)
