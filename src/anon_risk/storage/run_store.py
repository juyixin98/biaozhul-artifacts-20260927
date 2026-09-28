"""每次运行独立的加密状态存储（SQLite + cryptography Fernet）。

隔离模型
--------
- 每个运行一个独立 SQLite 文件 ``runs/<run_id>.db``：运行之间没有共享表、
  共享事务或共享临时状态，删除/损坏一个运行不影响其他运行。
- 文件内**所有原始数据**（行集、列声明、层级声明）是一个 Fernet 密文 blob，
  密钥由主密钥经 HKDF 以 (run_id, salt) 派生；salt 每运行随机生成。
- 访问令牌只存 SHA-256 摘要；令牌本身仅在创建时返回一次。
- 索引表 ``meta`` 里只放无敏感性的整数/短文本（行数、各列深度），供运行
  列表使用，无需解密整包。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet

from ..errors import ErrorCode, RiskError
from ..logging_setup import get_logger
from ..security import (
    decrypt_rowset, encrypt_rowset, new_access_token, new_run_id,
)

log = get_logger("storage")

_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class RunRecord:
    run_id: str
    token: Optional[str]          # 仅创建时有值
    salt: str
    created_at: str
    row_count: int
    qi_columns: list[str]
    sensitive_columns: list[str]


class RunStore:
    def __init__(self, runs_dir: str | Path, key_manager):
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.keys = key_manager

    # ---- 路径 / 连接 -------------------------------------------------
    def _path(self, run_id: str) -> Path:
        # 防穿越：run_id 仅允许 urlsafe 字符
        if not run_id or any(ch in run_id for ch in "/\\\n\t") or ".." in run_id:
            raise RiskError("非法 run_id", code=ErrorCode.INVALID_PARAMETER)
        return self.runs_dir / f"{run_id}.db"

    def _connect(self, run_id: str) -> sqlite3.Connection:
        path = self._path(run_id)
        if not path.exists():
            raise RiskError(
                f"运行 {run_id} 不存在",
                code=ErrorCode.RUN_NOT_FOUND, http_status=404,
                details={"run_id_present": False},
            )
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.executescript(_RUN_SCHEMA)
        return conn

    def _fernet(self, run_id: str, salt: str) -> Fernet:
        return self.keys.fernet_for_run(run_id, salt)

    # ---- 创建 / 打开 -------------------------------------------------
    def create_run(self, dataset_payload: dict[str, Any], *,
                  metric_version: str) -> RunRecord:
        run_id = new_run_id()
        token = new_access_token()
        salt = os.urandom(16).hex()
        created_at = _now()
        n_rows = len(dataset_payload["rows"])

        conn = self._open_new(run_id)
        try:
            blob = encrypt_rowset(
                self._fernet(run_id, salt),
                json.dumps(dataset_payload, ensure_ascii=False).encode("utf-8"),
            )
            meta = {
                "run_id": run_id,
                "salt": salt,
                "created_at": created_at,
                "row_count": str(n_rows),
                "qi_columns": json.dumps(dataset_payload["quasi_identifiers"],
                                         ensure_ascii=False),
                "sensitive_columns": json.dumps(dataset_payload["sensitive"],
                                                ensure_ascii=False),
                "token_sha256": _token_digest(token),
                "metric_version": metric_version,
                "data_blob": blob.hex(),
            }
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                list(meta.items()),
            )
            conn.commit()
        finally:
            conn.close()

        log.info("创建运行", extra={"event": {
            "run_id": run_id, "rows": n_rows, "salted": True}})
        return RunRecord(
            run_id=run_id, token=token, salt=salt, created_at=created_at,
            row_count=n_rows,
            qi_columns=list(dataset_payload["quasi_identifiers"]),
            sensitive_columns=list(dataset_payload["sensitive"]),
        )

    def _open_new(self, run_id: str) -> sqlite3.Connection:
        path = self._path(run_id)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        conn.executescript(_RUN_SCHEMA)
        return conn

    def _load_meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}

    def authenticate(self, conn: sqlite3.Connection, token: str) -> dict:
        meta = self._load_meta(conn)
        stored = meta.get("token_sha256", "")
        if not token or not hmac.compare_digest(stored, _token_digest(token)):
            raise RiskError(
                "访问令牌缺失或不匹配",
                code=ErrorCode.RUN_FORBIDDEN, http_status=403,
            )
        return meta

    def open_run(self, run_id: str, token: str):
        """校验令牌并返回 (连接, 元数据, 解密后的数据集 dict)。"""
        conn = self._connect(run_id)
        meta = self.authenticate(conn, token)
        fernet = self._fernet(run_id, meta["salt"])
        try:
            raw = decrypt_rowset(fernet, bytes.fromhex(meta["data_blob"]))
            dataset_payload = json.loads(raw.decode("utf-8"))
        except RiskError:
            conn.close()
            raise
        return conn, meta, dataset_payload

    def get_summary(self, run_id: str) -> dict:
        """无需令牌的运行摘要（仅索引元数据，不含任何数据值）。"""
        conn = self._connect(run_id)
        try:
            meta = self._load_meta(conn)
            return {
                "run_id": run_id,
                "created_at": meta.get("created_at"),
                "row_count": int(meta.get("row_count", "0")),
                "qi_columns": json.loads(meta.get("qi_columns", "[]")),
                "sensitive_columns": json.loads(meta.get("sensitive_columns", "[]")),
                "metric_version": meta.get("metric_version"),
            }
        finally:
            conn.close()

    def list_runs(self) -> list[dict]:
        out = []
        for p in sorted(self.runs_dir.glob("*.db")):
            try:
                out.append(self.get_summary(p.stem))
            except RiskError:
                continue
        return out

    def record_operation(self, conn: sqlite3.Connection, kind: str,
                         status: str, summary: dict) -> None:
        conn.execute(
            "INSERT INTO operations(ts, kind, status, summary_json) VALUES (?, ?, ?, ?)",
            (_now(), kind, status,
             json.dumps(summary, ensure_ascii=False, sort_keys=True)),
        )
        conn.commit()

    def list_operations(self, run_id: str, token: str, limit: int = 100) -> dict:
        conn = self._connect(run_id)
        try:
            self.authenticate(conn, token)
            rows = conn.execute(
                "SELECT id, ts, kind, status, summary_json FROM operations "
                "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),),
            ).fetchall()
            return {"operations": [{
                "id": r["id"], "ts": r["ts"], "kind": r["kind"],
                "status": r["status"],
                "summary": json.loads(r["summary_json"] or "{}"),
            } for r in rows]}
        finally:
            conn.close()

    def delete_run(self, run_id: str, token: str) -> None:
        conn = self._connect(run_id)
        try:
            self.authenticate(conn, token)
        finally:
            conn.close()
        self._path(run_id).unlink()
        log.info("删除运行", extra={"event": {"run_id": run_id}})

    def exists(self, run_id: str) -> bool:
        return self._path(run_id).exists()
