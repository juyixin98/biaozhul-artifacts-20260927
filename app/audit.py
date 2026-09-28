"""审计接口的存储与签名层。

* SQLite（WAL）保存每个 run 的终态与规范 JSON 摘要；
* JSONL 事件流（``events.jsonl``）保留 begin/finish 原始记录，便于离线重放；
* 运行编号、证据链、失败类别/原因码原样持久化；
* 使用 :mod:`cryptography` 的 Ed25519 对 run 的**规范序列化**签名，
  公钥写入 ``public_key.pem``，任何持有公钥者可通过 :meth:`verify_run` 复核
  证据未被篡改（签名不代表请求“合法”，只代表记录来自本内核且未被改写）。

状态隔离：每个 :class:`AuditStore` 实例对应独立目录；键为 run_id。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .contracts import RunResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    requested_url TEXT NOT NULL,
    verdict TEXT NOT NULL,
    status TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    failure_kind TEXT,
    failure_reason TEXT,
    hops INTEGER NOT NULL,
    evidence_count INTEGER NOT NULL,
    canonical_json TEXT NOT NULL,
    signature_b64 TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_start ON runs(start_ts DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
"""


def canonical_json(obj: Any) -> str:
    """规范序列化：sort_keys、无空白、确保跨进程字节一致。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_run(result: RunResult | dict[str, Any]) -> bytes:
    data = result.to_dict() if isinstance(result, RunResult) else result
    # 签名字段本身不参与摘要
    data = dict(data)
    data.pop("signature_b64", None)
    return hashlib.sha256(canonical_json(data).encode("utf-8")).digest()


class AuditStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "audit.sqlite3"
        self.events_path = self.dir / "events.jsonl"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._private_key = self._load_or_create_key()

    # -- AuditSink 协议 ----------------------------------------------------

    def begin_run(self, result: RunResult) -> None:
        self._append_event(result.run_id, "begin", {
            "run_id": result.run_id,
            "requested_url": result.requested_url,
            "start_ts": result.start_ts,
            "policy_source": result.policy_file,
        })

    def finish_run(self, result: RunResult) -> None:
        data = result.to_dict()
        signature = self.sign(data)
        data["signature_b64"] = signature
        failure = result.failure or {}
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, requested_url, verdict, status, start_ts, end_ts,
                    failure_kind, failure_reason, hops, evidence_count,
                    canonical_json, signature_b64)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.run_id,
                    result.requested_url,
                    result.verdict,
                    result.status,
                    result.start_ts,
                    result.end_ts,
                    failure.get("kind"),
                    failure.get("reason"),
                    len(result.hops),
                    len(result.evidence),
                    canonical_json(data),
                    signature,
                ),
            )
            self._conn.commit()
        self._append_event(result.run_id, "finish", data)

    # -- 查询接口（webapi 使用） -------------------------------------------

    def list_runs(self, limit: int = 50, *, verdict: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT run_id, requested_url, verdict, status, start_ts, end_ts, "
               "failure_kind, failure_reason, hops, evidence_count FROM runs")
        params: list[Any] = []
        if verdict:
            sql += " WHERE verdict = ?"
            params.append(verdict)
        sql += " ORDER BY start_ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "run_id": r[0], "requested_url": r[1], "verdict": r[2], "status": r[3],
                "start_ts": r[4], "end_ts": r[5], "failure_kind": r[6],
                "failure_reason": r[7], "hops": r[8], "evidence_count": r[9],
            }
            for r in rows
        ]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT canonical_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def verify_run(self, run_id: str, *, public_key_pem: bytes | None = None) -> dict[str, Any]:
        data = self.get_run(run_id)
        if data is None:
            return {"run_id": run_id, "found": False, "valid": False}
        signature = data.get("signature_b64")
        key_pem = public_key_pem or self.public_key_pem()
        valid = self._verify(data, signature, key_pem)
        return {
            "run_id": run_id,
            "found": True,
            "valid": valid,
            "digest_sha256": digest_run(data).hex(),
            "verified_with": "provided_key" if public_key_pem else "local_public_key",
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 密钥与签名 --------------------------------------------------------

    def public_key_pem(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def sign(self, data: dict[str, Any]) -> str:
        import base64

        payload = dict(data)
        payload.pop("signature_b64", None)
        sig = self._private_key.sign(canonical_json(payload).encode("utf-8"))
        return base64.b64encode(sig).decode("ascii")

    @staticmethod
    def _verify(data: dict[str, Any], signature_b64: str | None, public_key_pem: bytes) -> bool:
        import base64

        if not signature_b64:
            return False
        try:
            key = serialization.load_pem_public_key(public_key_pem)
            assert isinstance(key, Ed25519PublicKey)
            payload = dict(data)
            payload.pop("signature_b64", None)
            key.verify(base64.b64decode(signature_b64), canonical_json(payload).encode("utf-8"))
            return True
        except (InvalidSignature, ValueError, AssertionError):
            return False

    def _load_or_create_key(self) -> Ed25519PrivateKey:
        key_path = self.dir / "ed25519_private.pem"
        pub_path = self.dir / "public_key.pem"
        if key_path.exists():
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            assert isinstance(key, Ed25519PrivateKey)
            return key
        key = Ed25519PrivateKey.generate()
        key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        key_path.chmod(0o600)
        pub_path.write_bytes(key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
        return key

    # -- 内部 --------------------------------------------------------------

    def _append_event(self, run_id: str, phase: str, payload: dict[str, Any]) -> None:
        import time

        line = canonical_json({
            "run_id": run_id,
            "phase": phase,
            "ts": time.time(),
            "payload": payload,
        })
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._conn.execute(
                "INSERT INTO events (run_id, phase, ts, payload) VALUES (?,?,?,?)",
                (run_id, phase, time.time(), canonical_json(payload)),
            )
            self._conn.commit()
