"""SQLite 状态层：不可变策略/证据快照、差分结果、审计日志。

隔离约定：
- 策略一经写入不可更新（version 唯一，重复提交报 SCHEMA_VERSION_CONFLICT）。
- 审计日志只追加；所有行带 request_id / actor / version / component。
- 连接使用 WAL + 外键；store 不做任何安全判定。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import (
    FailureKind,
    VersionConflictError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
    version        TEXT PRIMARY KEY,
    source_hash    TEXT NOT NULL,
    submitted_by   TEXT,
    envelope_json  TEXT NOT NULL,
    parsed_json    TEXT NOT NULL,
    received_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_bundles (
    bundle_id      TEXT PRIMARY KEY,
    submitted_by   TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    scope_json     TEXT NOT NULL,
    record_count   INTEGER NOT NULL,
    envelope_json  TEXT NOT NULL,
    received_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diffs (
    diff_id        TEXT PRIMARY KEY,
    request_id     TEXT NOT NULL,
    actor          TEXT NOT NULL,
    old_version    TEXT NOT NULL,
    new_version    TEXT NOT NULL,
    scope_json     TEXT NOT NULL,
    space_size     INTEGER NOT NULL,
    enumeration_count INTEGER NOT NULL,
    summary_json   TEXT NOT NULL,
    witnesses_json TEXT NOT NULL,
    evidence_json  TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    component   TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    version     TEXT,
    diff_id     TEXT,
    summary     TEXT,
    detail_json TEXT,
    at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_diff ON audit_log(diff_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 单把锁：FastAPI 线程池下串行化写入
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 策略快照
    # ------------------------------------------------------------------
    def save_policy(
        self,
        *,
        version: str,
        source_hash: str,
        submitted_by: str,
        envelope: dict[str, Any],
        parsed: dict[str, Any],
    ) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO policies(version, source_hash, submitted_by, "
                    "envelope_json, parsed_json, received_at) VALUES (?,?,?,?,?,?)",
                    (
                        version,
                        source_hash,
                        submitted_by,
                        json.dumps(envelope, ensure_ascii=False),
                        json.dumps(parsed, ensure_ascii=False),
                        utc_now(),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                raise VersionConflictError(
                    f"策略版本 {version!r} 已存在；策略快照不可覆盖",
                    {"version": version},
                ) from None

    def get_policy_envelope(self, version: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT envelope_json FROM policies WHERE version=?", (version,)
            ).fetchone()
        return json.loads(row["envelope_json"]) if row else None

    def get_policy_parsed(self, version: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT parsed_json FROM policies WHERE version=?", (version,)
            ).fetchone()
        return json.loads(row["parsed_json"]) if row else None

    def list_policies(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, source_hash, submitted_by, received_at "
                "FROM policies ORDER BY received_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 证据快照
    # ------------------------------------------------------------------
    def save_evidence_bundle(
        self,
        *,
        bundle_id: str,
        submitted_by: str,
        policy_version: str,
        scope: dict[str, Any],
        record_count: int,
        envelope: dict[str, Any],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO evidence_bundles(bundle_id, submitted_by, "
                "policy_version, scope_json, record_count, envelope_json, received_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    bundle_id,
                    submitted_by,
                    policy_version,
                    json.dumps(scope, ensure_ascii=False),
                    record_count,
                    json.dumps(envelope, ensure_ascii=False),
                    utc_now(),
                ),
            )
            self._conn.commit()

    def get_evidence_envelope(self, bundle_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT envelope_json FROM evidence_bundles WHERE bundle_id=?",
                (bundle_id,),
            ).fetchone()
        return json.loads(row["envelope_json"]) if row else None

    # ------------------------------------------------------------------
    # 差分结果
    # ------------------------------------------------------------------
    def save_diff(
        self,
        *,
        diff_id: str,
        request_id: str,
        actor: str,
        old_version: str,
        new_version: str,
        scope: dict[str, Any],
        space_size: int,
        enumeration_count: int,
        summary: dict[str, Any],
        witnesses: dict[str, Any],
        evidence: Optional[dict[str, Any]],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO diffs(diff_id, request_id, actor, old_version, "
                "new_version, scope_json, space_size, enumeration_count, "
                "summary_json, witnesses_json, evidence_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    diff_id,
                    request_id,
                    actor,
                    old_version,
                    new_version,
                    json.dumps(scope, ensure_ascii=False),
                    space_size,
                    enumeration_count,
                    json.dumps(summary, ensure_ascii=False),
                    json.dumps(witnesses, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False) if evidence else None,
                    utc_now(),
                ),
            )
            self._conn.commit()

    def get_diff(self, diff_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM diffs WHERE diff_id=?", (diff_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("scope_json", "summary_json", "witnesses_json", "evidence_json"):
            d[k.removesuffix("_json")] = json.loads(d.pop(k)) if d[k] else None
        return d

    def list_diffs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT diff_id, request_id, actor, old_version, new_version, "
                "space_size, enumeration_count, created_at FROM diffs "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 审计日志（只追加）
    # ------------------------------------------------------------------
    def append_audit(
        self,
        *,
        request_id: str,
        actor: str,
        component: str,
        stage: str,
        status: str,
        version: Optional[str] = None,
        diff_id: Optional[str] = None,
        summary: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit_log(request_id, actor, component, stage, status, "
                "version, diff_id, summary, detail_json, at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    actor,
                    component,
                    stage,
                    status,
                    version,
                    diff_id,
                    summary,
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                    utc_now(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def query_audit(
        self,
        *,
        request_id: Optional[str] = None,
        diff_id: Optional[str] = None,
        actor: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        args: list[Any] = []
        if request_id:
            sql += " AND request_id=?"
            args.append(request_id)
        if diff_id:
            sql += " AND diff_id=?"
            args.append(diff_id)
        if actor:
            sql += " AND actor=?"
            args.append(actor)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json")) if d["detail_json"] else None
            out.append(d)
        return out


def failure_status(kind: FailureKind) -> str:
    """失败类别 -> 审计状态（失败与不确定单列）。"""
    if kind.name.startswith("CRYPTO"):
        return "FAILURE_CRYPTO"
    if kind.name.startswith("SCOPE"):
        return "FAILURE_SCOPE"
    if kind.name.startswith("EVIDENCE"):
        return "FAILURE_EVIDENCE"
    if kind is FailureKind.NOT_FOUND:
        return "FAILURE_NOT_FOUND"
    return "FAILURE_SCHEMA"
