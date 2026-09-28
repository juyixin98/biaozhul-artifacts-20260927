"""SQLite 持久层：状态隔离、证据加密落盘、审计事件哈希链。

状态机：open（可写策略/证据）→ sealed（封口，只读）。
已有证据后策略锁定不可变；所有写操作经同一把锁串行化。
敏感字段（Authorization/Cookie/响应体）使用每运行派生的 Fernet 密钥加密。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .crypto import GENESIS_HASH, RunCrypto
from .errors import (
    ErrorCode,
    InputError,
    ResourceExhaustedError,
    StateConflictError,
)
from .models import Evidence, Policy
from .parser import decode_body, verify_body_hash

MAX_EVIDENCE_PER_RUN = 200
MAX_BODY_BYTES = 64 * 1024
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    policy_json TEXT,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    analyzed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    run_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, evidence_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS analyses (
    run_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT 'current',
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, label)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_run_id(run_id: str) -> None:
    if not RUN_ID_RE.match(run_id):
        raise InputError(
            "run_id 只允许字母数字及 _.-，且须以字母数字开头，最长 64",
            code=ErrorCode.RUN_ID_INVALID,
        )


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = dict(headers)
    for h in ("Authorization", "Cookie"):
        if h in redacted:
            redacted[h] = "<redacted:stored-encrypted>"
    return redacted


class AuditStorage:
    def __init__(self, db_path: str | Path, master_key: str | bytes):
        self.db_path = str(db_path)
        self._master_key = master_key
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 事件链 ----------
    def _append_event(self, conn, run_id: str, event_type: str,
                      payload: dict[str, Any]) -> dict[str, Any]:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE run_id=?",
            (run_id,),
        ).fetchone()["n"]
        prev = conn.execute(
            "SELECT entry_hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        prev_hash = prev["entry_hash"] if prev else GENESIS_HASH
        ts = utc_now()
        rc = RunCrypto(self._master_key, run_id)
        entry_hash = rc.chain_hash(seq, event_type, ts, payload, prev_hash)
        conn.execute(
            "INSERT INTO events(run_id,seq,event_type,timestamp,payload_json,"
            "prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?)",
            (run_id, seq, event_type, ts, json.dumps(payload, ensure_ascii=False),
             prev_hash, entry_hash),
        )
        return {"seq": seq, "event_type": event_type, "timestamp": ts,
                "payload": payload, "prev_hash": prev_hash, "entry_hash": entry_hash}

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        self.require_run(run_id)
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [
            {"seq": r["seq"], "event_type": r["event_type"], "timestamp": r["timestamp"],
             "payload": json.loads(r["payload_json"]),
             "prev_hash": r["prev_hash"], "entry_hash": r["entry_hash"]}
            for r in rows
        ]

    def verify_chain(self, run_id: str) -> dict[str, Any]:
        self.require_run(run_id)
        rc = RunCrypto(self._master_key, run_id)
        prev_hash = GENESIS_HASH
        first_mismatch: int | None = None
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        for r in rows:
            payload = json.loads(r["payload_json"])
            expect = rc.chain_hash(r["seq"], r["event_type"], r["timestamp"],
                                   payload, prev_hash)
            if expect != r["entry_hash"] or r["prev_hash"] != prev_hash:
                first_mismatch = r["seq"]
                break
            prev_hash = r["entry_hash"]
        return {"run_id": run_id, "ok": first_mismatch is None,
                "entries": len(rows), "first_mismatch_seq": first_mismatch}

    # ---------- 运行 ----------
    def create_run(self, run_id: str | None, label: str = "") -> dict[str, Any]:
        run_id = run_id or f"run-{uuid.uuid4().hex[:16]}"
        validate_run_id(run_id)
        with self._lock:
            try:
                existing = self._conn.execute(
                    "SELECT run_id FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if existing:
                    raise StateConflictError(
                        f"运行 {run_id} 已存在", code=ErrorCode.RUN_EXISTS,
                        details={"run_id": run_id},
                    )
                ts = utc_now()
                self._conn.execute(
                    "INSERT INTO runs(run_id,label,status,created_at,updated_at)"
                    " VALUES(?,?, 'open',?,?)",
                    (run_id, label, ts, ts),
                )
                self._append_event(self._conn, run_id, "run_created",
                                   {"label": label})
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_run(run_id)

    def require_run(self, run_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise StateConflictError(
                f"运行 {run_id} 不存在", code=ErrorCode.RUN_NOT_FOUND,
                details={"run_id": run_id},
            )
        return row

    def get_run(self, run_id: str) -> dict[str, Any]:
        r = self.require_run(run_id)
        return {"run_id": r["run_id"], "label": r["label"], "status": r["status"],
                "evidence_count": r["evidence_count"],
                "has_policy": r["policy_json"] is not None,
                "analyzed": bool(r["analyzed"]),
                "created_at": r["created_at"]}

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise InputError("limit 必须在 1..200", code=ErrorCode.PAGINATION_INVALID)
        if offset < 0:
            raise InputError("offset 不能为负", code=ErrorCode.PAGINATION_INVALID)
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [{"run_id": r["run_id"], "label": r["label"], "status": r["status"],
                 "evidence_count": r["evidence_count"],
                 "has_policy": r["policy_json"] is not None,
                 "analyzed": bool(r["analyzed"]),
                 "created_at": r["created_at"]} for r in rows]

    def seal_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            r = self.require_run(run_id)
            if r["status"] != "open":
                raise StateConflictError(
                    "运行已封口，无法重复封口", code=ErrorCode.RUN_NOT_OPEN,
                    details={"run_id": run_id, "status": r["status"]},
                )
            ts = utc_now()
            self._conn.execute(
                "UPDATE runs SET status='sealed', updated_at=? WHERE run_id=?",
                (ts, run_id),
            )
            self._append_event(self._conn, run_id, "run_sealed", {})
            self._conn.commit()
        return self.get_run(run_id)

    # ---------- 策略 ----------
    def set_policy(self, run_id: str, policy: Policy) -> dict[str, Any]:
        with self._lock:
            r = self.require_run(run_id)
            if r["status"] != "open":
                raise StateConflictError(
                    "运行已封口，不能修改策略", code=ErrorCode.RUN_NOT_OPEN)
            if r["evidence_count"] > 0:
                raise StateConflictError(
                    "已有证据提交，策略已锁定；请新建运行以审计不同策略",
                    code=ErrorCode.POLICY_LOCKED,
                    details={"evidence_count": r["evidence_count"]},
                )
            ts = utc_now()
            self._conn.execute(
                "UPDATE runs SET policy_json=?, updated_at=? WHERE run_id=?",
                (policy.model_dump_json(), ts, run_id),
            )
            self._append_event(self._conn, run_id, "policy_set",
                               {"policy": policy.model_dump(mode="json")})
            self._conn.commit()
        return self.get_run(run_id)

    def get_policy(self, run_id: str) -> Policy:
        r = self.require_run(run_id)
        if not r["policy_json"]:
            raise StateConflictError(
                "该运行尚未配置策略", code=ErrorCode.POLICY_NOT_SET,
                details={"run_id": run_id},
            )
        return Policy.model_validate(json.loads(r["policy_json"]))

    # ---------- 证据 ----------
    def _require_writable_with_policy(self, run_id: str) -> sqlite3.Row:
        r = self.require_run(run_id)
        if r["status"] != "open":
            raise StateConflictError("运行已封口，拒绝写入证据",
                                     code=ErrorCode.RUN_NOT_OPEN)
        if not r["policy_json"]:
            raise StateConflictError("请先配置策略，再提交证据",
                                     code=ErrorCode.POLICY_NOT_SET)
        return r

    def _encrypt_evidence(self, rc: RunCrypto, ev: Evidence) -> dict[str, Any]:
        verify_body_hash(ev.response.body_sha256, ev.response.body,
                         ev.response.body_encoding)
        data = ev.model_dump(mode="json")
        if "Authorization" in data["request"]["headers"]:
            data["request"]["headers"]["Authorization"] = rc.encrypt(
                data["request"]["headers"]["Authorization"])
        if "Cookie" in data["request"]["headers"]:
            data["request"]["headers"]["Cookie"] = rc.encrypt(
                data["request"]["headers"]["Cookie"])
        data["response"]["body"] = rc.encrypt(data["response"]["body"])
        return data

    def add_evidence(self, run_id: str, evidence_list: list[Evidence]) -> int:
        with self._lock:
            r = self._require_writable_with_policy(run_id)
            current = r["evidence_count"]
            if current + len(evidence_list) > MAX_EVIDENCE_PER_RUN:
                raise ResourceExhaustedError(
                    f"证据条数超过每运行上限 {MAX_EVIDENCE_PER_RUN}",
                    code=ErrorCode.TOO_MANY_EVIDENCE,
                    details={"current": current, "incoming": len(evidence_list),
                             "limit": MAX_EVIDENCE_PER_RUN},
                )
            rc = RunCrypto(self._master_key, run_id)
            added = 0
            try:
                for ev in evidence_list:
                    raw_body = decode_body(ev.response.body, ev.response.body_encoding)
                    if len(raw_body) > MAX_BODY_BYTES:
                        raise ResourceExhaustedError(
                            f"证据 {ev.id} 的响应体超过 {MAX_BODY_BYTES} 字节",
                            code=ErrorCode.EVIDENCE_TOO_LARGE,
                            details={"evidence_id": ev.id,
                                     "limit": MAX_BODY_BYTES},
                        )
                    exists = self._conn.execute(
                        "SELECT 1 FROM evidence WHERE run_id=? AND evidence_id=?",
                        (run_id, ev.id),
                    ).fetchone()
                    if exists:
                        raise InputError(
                            f"证据 id {ev.id} 在本运行内重复",
                            code=ErrorCode.EVIDENCE_INVALID,
                            details={"evidence_id": ev.id},
                        )
                    encrypted = self._encrypt_evidence(rc, ev)
                    seq = current + added + 1
                    self._conn.execute(
                        "INSERT INTO evidence(run_id,evidence_id,seq,payload_json)"
                        " VALUES(?,?,?,?)",
                        (run_id, ev.id, seq,
                         json.dumps(encrypted, ensure_ascii=False)),
                    )
                    self._append_event(self._conn, run_id, "evidence_added", {
                        "evidence_id": ev.id,
                        "seq": seq,
                        "source": ev.source,
                        "request": {
                            "method": ev.request.method,
                            "scheme": ev.request.scheme,
                            "host": ev.request.host,
                            "path": ev.request.path,
                            "query": ev.request.query,
                            "headers": _redact_headers(ev.request.headers),
                        },
                        "response": {
                            "status": ev.response.status,
                            "headers": dict(ev.response.headers),
                            "body_sha256": ev.response.body_sha256,
                        },
                    })
                    added += 1
                ts = utc_now()
                self._conn.execute(
                    "UPDATE runs SET evidence_count=?, updated_at=? WHERE run_id=?",
                    (current + added, ts, run_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return current + added

    def list_evidence(self, run_id: str, decrypt: bool = True) -> list[Evidence]:
        self.require_run(run_id)
        rows = self._conn.execute(
            "SELECT payload_json FROM evidence WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
        if not decrypt:
            return [Evidence.model_validate(json.loads(r["payload_json"])) for r in rows]
        rc = RunCrypto(self._master_key, run_id)
        out: list[Evidence] = []
        for r in rows:
            data = json.loads(r["payload_json"])
            for h in ("Authorization", "Cookie"):
                if h in data["request"]["headers"]:
                    data["request"]["headers"][h] = rc.decrypt(
                        data["request"]["headers"][h])
            data["response"]["body"] = rc.decrypt(data["response"]["body"])
            out.append(Evidence.model_validate(data))
        return out

    # ---------- 分析结果 ----------
    def save_analysis(self, run_id: str, label: str, result: dict[str, Any]) -> None:
        with self._lock:
            self.require_run(run_id)
            ts = utc_now()
            self._conn.execute(
                "INSERT INTO analyses(run_id,label,result_json,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(run_id,label) DO UPDATE SET "
                "result_json=excluded.result_json, created_at=excluded.created_at",
                (run_id, label, json.dumps(result, ensure_ascii=False), ts),
            )
            self._conn.execute(
                "UPDATE runs SET analyzed=1, updated_at=? WHERE run_id=?",
                (ts, run_id),
            )
            self._append_event(self._conn, run_id, "analysis_saved", {
                "label": label,
                "collision_groups": result.get("collision_groups"),
                "findings": len(result.get("findings", [])),
            })
            self._conn.commit()

    def get_analysis(self, run_id: str, label: str = "current") -> dict[str, Any]:
        self.require_run(run_id)
        row = self._conn.execute(
            "SELECT result_json FROM analyses WHERE run_id=? AND label=?",
            (run_id, label),
        ).fetchone()
        if not row:
            raise StateConflictError(
                "该运行尚无分析结果，请先执行分析", code=ErrorCode.POLICY_NOT_SET,
                details={"run_id": run_id, "label": label},
            )
        return json.loads(row["result_json"])

    def has_analysis(self, run_id: str, label: str = "current") -> bool:
        self.require_run(run_id)
        return self._conn.execute(
            "SELECT 1 FROM analyses WHERE run_id=? AND label=?", (run_id, label)
        ).fetchone() is not None

    def save_remediation_event(self, run_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self.require_run(run_id)
            self._append_event(self._conn, run_id, "remediation_verified", {
                "collision_gone": result["collision_gone"],
                "cleared": len(result["cleared_witness_ids"]),
                "residual": len(result["residual_witness_ids"]),
            })
            self._conn.commit()
