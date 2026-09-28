"""SQLite 持久化层。

状态隔离
========
* 每次操作使用独立短连接（``check_same_thread=False`` + WAL + 写锁），
  请求间不共享可变会话；
* 提交的原始行**仅以密文**落盘（Fernet），可读列只存元数据/指纹；
* 分析结果为不可变快照：运行创建后不再修改（仅追加时间戳）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.errors import FailureCode, ServiceError
from app.security.crypto import CryptoBox

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schemas (
    schema_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    key_source    TEXT NOT NULL,
    column_roles  TEXT NOT NULL,   -- JSON: [{name, role, height}]（不含数据）
    n_rows        INTEGER NOT NULL,
    k             INTEGER NOT NULL,
    l             INTEGER NOT NULL,
    ciphertext    BLOB NOT NULL,   -- Fernet(规范化提交数据)
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    schema_id     TEXT NOT NULL REFERENCES schemas(schema_id),
    status        TEXT NOT NULL,
    failure_code  TEXT,
    k             INTEGER NOT NULL,
    l             INTEGER NOT NULL,
    result_json   TEXT,            -- 完整结果快照（已脱敏，不含真实值）
    n_combinations_explored INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_schema ON runs(schema_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path, crypto: CryptoBox) -> None:
        self.path = Path(path) if str(path) != ":memory:" else ":memory:"
        if self.path != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.crypto = crypto
        self._write_lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=30, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

    # ------------------------------------------------------------------ #
    def save_schema(
        self,
        schema_id: str,
        name: str,
        fingerprint: str,
        column_roles: list[dict[str, Any]],
        n_rows: int,
        k: int,
        l: int,
        secret_payload: Any,
    ) -> bool:
        """返回 True 表示新插入，False 表示同指纹已存在（幂等更新）。"""
        token = self.crypto.encrypt_json(secret_payload)
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existed = conn.execute(
                "SELECT 1 FROM schemas WHERE schema_id=?", (schema_id,)
            ).fetchone()
            conn.execute(
                """INSERT INTO schemas(schema_id, name, input_fingerprint, key_source,
                   column_roles, n_rows, k, l, ciphertext, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(schema_id) DO UPDATE SET
                     name=excluded.name,
                     column_roles=excluded.column_roles,
                     n_rows=excluded.n_rows,
                     k=excluded.k,
                     l=excluded.l,
                     ciphertext=excluded.ciphertext""",
                (
                    schema_id,
                    name,
                    fingerprint,
                    self.crypto.key_source,
                    json.dumps(column_roles, ensure_ascii=False),
                    n_rows,
                    k,
                    l,
                    token,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return existed is None

    def load_schema_secret(self, schema_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM schemas WHERE schema_id=?", (schema_id,)
            ).fetchone()
        if row is None:
            raise ServiceError(
                FailureCode.SCHEMA_NOT_FOUND, f"schema '{schema_id}' not found"
            )
        return self.crypto.decrypt_json(bytes(row["ciphertext"]))

    def get_schema_meta(self, schema_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT schema_id, name, input_fingerprint, key_source, column_roles, "
                "n_rows, k, l, created_at FROM schemas WHERE schema_id=?",
                (schema_id,),
            ).fetchone()
        if row is None:
            raise ServiceError(
                FailureCode.SCHEMA_NOT_FOUND, f"schema '{schema_id}' not found"
            )
        d = dict(row)
        d["column_roles"] = json.loads(d["column_roles"])
        return d

    def list_schemas(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT schema_id, name, input_fingerprint, key_source, column_roles, "
                "n_rows, k, l, created_at FROM schemas ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["column_roles"] = json.loads(d["column_roles"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ #
    def save_run(self, record: dict[str, Any]) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO runs(run_id, schema_id, status, failure_code, k, l,
                   result_json, n_combinations_explored, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    record["run_id"],
                    record["schema_id"],
                    record["status"],
                    record.get("failure_code"),
                    record["k"],
                    record["l"],
                    json.dumps(record, ensure_ascii=False),
                    record.get("n_combinations_explored", 0),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise ServiceError(FailureCode.RUN_NOT_FOUND, f"run '{run_id}' not found")
        return json.loads(row["result_json"])

    def list_runs(self, schema_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = "SELECT result_json FROM runs"
        args: tuple[Any, ...] = ()
        if schema_id:
            q += " WHERE schema_id=?"
            args = (schema_id,)
        q += " ORDER BY created_at DESC LIMIT ?"
        args = args + (limit,)
        with self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        return [json.loads(r["result_json"]) for r in rows]
