"""审计存储 —— SQLite 哈希链 + Ed25519 签名。

每条决策记录落库后串成只增哈希链：

    chain_hash_0 = SHA256(GENESIS || canonical_json(record_0))
    chain_hash_n = SHA256(chain_hash_{n-1} || canonical_json(record_n))
    signature_n = Ed25519_sign(chain_hash_n)

* 任意一条记录被改动，从该记录起的链全部对不上（篡改可检测）；
* 每条链哈希都有独立签名，验证不依赖在线私钥；
* WAL 模式 + 每次写入独立短连接，保证并发演示不丢记录；
* 验证返回第一个断链位置，便于复现定位。

状态隔离：所有查询以 ``run_id`` 为主键维度，不同运行互不串数据。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..errors import AuditChainBrokenError, AuditSigningError

GENESIS = b"safeproxy-audit-genesis-v1"


def generate_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, pub


def canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class AuditLog:
    def __init__(
        self,
        db_path: str,
        private_key: Ed25519PrivateKey | None = None,
        *,
        key_path: str | None = None,
    ) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 签名密钥必须跨进程持久，否则新进程无法验证旧记录。优先用显式传入，
        # 否则从侧车文件 <db>.key 加载；都没有则生成并落盘（权限 0600）。
        self._key_path = key_path or f"{db_path}.key"
        if private_key is not None:
            self._priv = private_key
        else:
            self._priv = self._load_or_create_key()
        self._pub = self._priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self._init_schema()

    def _load_or_create_key(self) -> Ed25519PrivateKey:
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            load_pem_private_key,
        )

        if os.path.exists(self._key_path):
            with open(self._key_path, "rb") as fh:
                key = load_pem_private_key(fh.read(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise AuditSigningError(f"审计密钥类型错误: {type(key)}")
            return key
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)
        return key

    # ------------------------------------------------------------------
    @property
    def public_key_hex(self) -> str:
        return self._pub.hex()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_runs (
                    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id       TEXT UNIQUE NOT NULL,
                    url          TEXT NOT NULL,
                    verdict      TEXT NOT NULL,
                    status_code  INTEGER,
                    error_code   TEXT,
                    error_cat    TEXT,
                    pinned       TEXT,
                    peer         TEXT,
                    body_sha256  TEXT,
                    record_json  TEXT NOT NULL,
                    chain_hash   TEXT NOT NULL,
                    signature    TEXT NOT NULL,
                    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_runs(run_id);
                CREATE INDEX IF NOT EXISTS idx_audit_verdict ON audit_runs(verdict);
                """
            )

    # ------------------------------------------------------------------
    def record(self, event: dict[str, Any]) -> None:
        """实现 kernel.AuditSink 协议。"""

        run_id = str(event.get("run_id", "unknown"))
        err = event.get("error") or {}
        row = {
            "run_id": run_id,
            "url": event.get("url", ""),
            "verdict": str(event.get("final_verdict", "")),
            "status_code": event.get("status_code"),
            "error_code": err.get("code") if isinstance(err, dict) else None,
            "error_cat": err.get("category") if isinstance(err, dict) else None,
            "pinned": json.dumps(event.get("pinned", []), ensure_ascii=False),
            "peer": json.dumps(event.get("connected_peer"), ensure_ascii=False),
            "body_sha256": event.get("body_sha256"),
            "record_json": json.dumps(event, ensure_ascii=False, sort_keys=True),
        }
        with self._lock:
            prev_hash = self._last_chain_hash()
            chain_hash = hashlib.sha256(prev_hash + canonical_json(event)).hexdigest()
            try:
                signature = self._priv.sign(bytes.fromhex(chain_hash)).hex()
            except Exception as exc:  # noqa: BLE001
                raise AuditSigningError(f"审计签名失败: {exc}") from exc
            row["chain_hash"] = chain_hash
            row["signature"] = signature
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO audit_runs
                      (run_id, url, verdict, status_code, error_code, error_cat,
                       pinned, peer, body_sha256, record_json, chain_hash, signature)
                    VALUES
                      (:run_id, :url, :verdict, :status_code, :error_code, :error_cat,
                       :pinned, :peer, :body_sha256, :record_json, :chain_hash, :signature)
                    """,
                    row,
                )

    def _last_chain_hash(self) -> bytes:
        with self._connect() as conn:
            cur = conn.execute("SELECT chain_hash FROM audit_runs ORDER BY seq DESC LIMIT 1")
            row = cur.fetchone()
        return bytes.fromhex(row["chain_hash"]) if row else GENESIS

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM audit_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50, verdict: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT run_id, url, verdict, status_code, error_code, error_cat, created_at FROM audit_runs"
        params: list[Any] = []
        if verdict:
            q += " WHERE verdict = ?"
            params.append(verdict)
        q += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def all_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM audit_runs ORDER BY seq ASC").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    def verify_chain(self) -> dict[str, Any]:
        """重算整条哈希链并校验每个签名。返回首个断点。"""

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        rows = self.all_rows()
        pub = Ed25519PublicKey.from_public_bytes(self._pub)
        prev = GENESIS
        for i, row in enumerate(rows):
            event = json.loads(row["record_json"])
            expected = hashlib.sha256(prev + canonical_json(event)).hexdigest()
            if expected != row["chain_hash"]:
                raise AuditChainBrokenError(
                    "审计哈希链断裂",
                    details={"index": i, "run_id": row["run_id"],
                             "expected": expected, "actual": row["chain_hash"]},
                )
            try:
                pub.verify(bytes.fromhex(row["signature"]), bytes.fromhex(row["chain_hash"]))
            except InvalidSignature as exc:
                raise AuditChainBrokenError(
                    "审计签名无效（记录可能被篡改）",
                    details={"index": i, "run_id": row["run_id"]},
                ) from exc
            prev = bytes.fromhex(expected)
        return {"ok": True, "records": len(rows), "public_key": self._pub.hex()}
