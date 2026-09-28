"""元数据事务层（SQLite）。

保存审计记录与逐条发现，所有写入在单个事务中提交（审计头与全部
findings 要么一起可见，要么一起回滚）。读取接口供验证 API 使用。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import AuditResult, Finding

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    audit_id      TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    summary       TEXT NOT NULL,
    trusted       TEXT NOT NULL,
    request_id    TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id      TEXT NOT NULL REFERENCES audits(audit_id) ON DELETE CASCADE,
    code          TEXT NOT NULL,
    severity      TEXT NOT NULL,
    locator       TEXT NOT NULL,
    message       TEXT NOT NULL,
    expected      TEXT,
    observed      TEXT,
    request_id    TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_audit ON findings(audit_id);
CREATE INDEX IF NOT EXISTS idx_audits_path ON audits(path);
CREATE INDEX IF NOT EXISTS idx_audits_request ON audits(request_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MetadataStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if parent and str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_audit(self, result: AuditResult, request_id: str | None = None) -> None:
        """原子写入审计头与全部 findings。"""
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO audits
                   (audit_id, path, verdict, summary, trusted, request_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.audit_id,
                    result.path,
                    result.verdict,
                    json.dumps(result.summary, ensure_ascii=False, sort_keys=True),
                    json.dumps(result.trusted, ensure_ascii=False, sort_keys=True),
                    request_id,
                    _now(),
                ),
            )
            conn.executemany(
                """INSERT INTO findings
                   (audit_id, code, severity, locator, message,
                    expected, observed, request_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        result.audit_id,
                        f.code,
                        f.severity.value,
                        json.dumps(f.locator, ensure_ascii=False, sort_keys=True),
                        f.message,
                        json.dumps(f.expected, ensure_ascii=False)
                        if f.expected is not None
                        else None,
                        json.dumps(f.observed, ensure_ascii=False)
                        if f.observed is not None
                        else None,
                        request_id,
                        _now(),
                    )
                    for f in result.findings
                ],
            )

    def get_audit(self, audit_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM audits WHERE audit_id = ?", (audit_id,)
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["summary"] = json.loads(data["summary"])
            data["trusted"] = json.loads(data["trusted"])
            data["findings"] = [
                dict(r) for r in conn.execute(
                    "SELECT code, severity, locator, message, expected, "
                    "observed, request_id FROM findings WHERE audit_id = ? "
                    "ORDER BY id",
                    (audit_id,),
                ).fetchall()
            ]
            for f in data["findings"]:
                f["locator"] = json.loads(f["locator"])
                if f["expected"]:
                    f["expected"] = json.loads(f["expected"])
                if f["observed"]:
                    f["observed"] = json.loads(f["observed"])
            return data

    def list_audits(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT audit_id, path, verdict, request_id, created_at "
                "FROM audits ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_by_request(self, request_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT audit_id, path, verdict, created_at FROM audits "
                "WHERE request_id = ? ORDER BY created_at",
                (request_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def latest_for_path(self, path: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT audit_id FROM audits WHERE path = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (path,),
            ).fetchone()
            if row is None:
                return None
            return self.get_audit(row["audit_id"])
