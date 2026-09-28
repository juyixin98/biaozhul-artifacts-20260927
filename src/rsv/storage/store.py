"""索引存储：SQLite 实现（模块三）。

职责仅限持久化与索引，不含任何脚本/验签逻辑：
- utxo 表：链上当前未花费输出（按 txid+index 主键）；
- spent 表：花费记录（用于双花判定与审计回放）；
- runs 表：每次验证/提交的结构化运行记录（失败分类、轨迹、判断理由）；
- meta 表：schema 版本、创世纪 id、状态根等。

Store 接口是抽象的，离线回放使用同一内核 + 内存 SQLite，保证两条路径一致。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS utxo (
    txid          TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    value         INTEGER NOT NULL,
    pubkey_script TEXT NOT NULL,   -- hex
    domain        TEXT NOT NULL,
    created_run   TEXT NOT NULL,
    PRIMARY KEY (txid, idx)
);
CREATE TABLE IF NOT EXISTS spent (
    txid        TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    spent_txid  TEXT NOT NULL,
    spent_run   TEXT NOT NULL,
    PRIMARY KEY (txid, idx)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- verify | submit | replay
    accepted    INTEGER NOT NULL,
    txid        TEXT,
    category    TEXT,
    code        TEXT,
    detail      TEXT,
    message32   TEXT,
    trace       TEXT,               -- JSON array
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_utxo_domain ON utxo(domain);
CREATE INDEX IF NOT EXISTS idx_spent_tx ON spent(spent_txid);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts);
"""


@dataclass(frozen=True)
class UTXORecord:
    txid: str
    idx: int
    value: int
    pubkey_script: str  # hex
    domain: str
    created_run: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    ts: str
    kind: str
    accepted: bool
    txid: str | None
    category: str | None
    code: str | None
    detail: str | None
    message32: str | None
    trace: list[str]
    reason: str | None


class Store(Protocol):
    def connect(self) -> sqlite3.Connection: ...
    def initialize(self) -> None: ...
    def get_utxo(self, txid: str, idx: int) -> UTXORecord | None: ...
    def is_spent(self, txid: str, idx: int) -> bool: ...
    def add_utxo(self, rec: UTXORecord) -> None: ...
    def mark_spent(self, txid: str, idx: int, spent_txid: str, run_id: str) -> None: ...
    def list_utxos(self, domain: str | None = None) -> list[UTXORecord]: ...
    def insert_run(self, rec: RunRecord) -> None: ...
    def get_run(self, run_id: str) -> RunRecord | None: ...
    def state_root(self) -> str: ...
    def set_meta(self, key: str, value: str) -> None: ...
    def get_meta(self, key: str) -> str | None: ...


class SQLiteStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._mem: sqlite3.Connection | None = None
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            if self._mem is None:
                self._mem = sqlite3.connect(":memory:", isolation_level=None)
                self._mem.row_factory = sqlite3.Row
            return self._mem
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def initialize(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", "1")

    # -- UTXO -------------------------------------------------------------
    def get_utxo(self, txid: str, idx: int) -> UTXORecord | None:
        row = self.connect().execute(
            "SELECT * FROM utxo WHERE txid=? AND idx=?", (txid, idx)
        ).fetchone()
        return _row_to_utxo(row) if row else None

    def is_spent(self, txid: str, idx: int) -> bool:
        row = self.connect().execute(
            "SELECT 1 FROM spent WHERE txid=? AND idx=?", (txid, idx)
        ).fetchone()
        return row is not None

    def add_utxo(self, rec: UTXORecord) -> None:
        self.connect().execute(
            "INSERT INTO utxo(txid, idx, value, pubkey_script, domain, created_run)"
            " VALUES (?,?,?,?,?,?)",
            (rec.txid, rec.idx, rec.value, rec.pubkey_script, rec.domain, rec.created_run),
        )

    def mark_spent(self, txid: str, idx: int, spent_txid: str, run_id: str) -> None:
        self.connect().execute(
            "INSERT INTO spent(txid, idx, spent_txid, spent_run) VALUES (?,?,?,?)",
            (txid, idx, spent_txid, run_id),
        )
        self.connect().execute(
            "DELETE FROM utxo WHERE txid=? AND idx=?", (txid, idx)
        )

    def list_utxos(self, domain: str | None = None) -> list[UTXORecord]:
        conn = self.connect()
        if domain is None:
            rows = conn.execute("SELECT * FROM utxo ORDER BY txid, idx").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM utxo WHERE domain=? ORDER BY txid, idx", (domain,)
            ).fetchall()
        return [_row_to_utxo(r) for r in rows]

    # -- runs -------------------------------------------------------------
    def insert_run(self, rec: RunRecord) -> None:
        self.connect().execute(
            "INSERT INTO runs(run_id, ts, kind, accepted, txid, category, code, detail,"
            " message32, trace, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.run_id,
                rec.ts,
                rec.kind,
                1 if rec.accepted else 0,
                rec.txid,
                rec.category,
                rec.code,
                rec.detail,
                rec.message32,
                json.dumps(rec.trace, ensure_ascii=False),
                rec.reason,
            ),
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.connect().execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        return RunRecord(
            run_id=row["run_id"],
            ts=row["ts"],
            kind=row["kind"],
            accepted=bool(row["accepted"]),
            txid=row["txid"],
            category=row["category"],
            code=row["code"],
            detail=row["detail"],
            message32=row["message32"],
            trace=json.loads(row["trace"]),
            reason=row["reason"],
        )

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        rows = self.connect().execute(
            "SELECT * FROM runs ORDER BY ts DESC, run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            RunRecord(
                run_id=r["run_id"], ts=r["ts"], kind=r["kind"], accepted=bool(r["accepted"]),
                txid=r["txid"], category=r["category"], code=r["code"], detail=r["detail"],
                message32=r["message32"], trace=json.loads(r["trace"]), reason=r["reason"],
            )
            for r in rows
        ]

    # -- meta / root ------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.connect().execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def state_root(self) -> str:
        """对当前 UTXO 集合做确定性 SHA256，供离线回放对账。"""
        import hashlib

        rows = self.connect().execute(
            "SELECT txid, idx, value, pubkey_script, domain FROM utxo ORDER BY txid, idx"
        ).fetchall()
        h = hashlib.sha256()
        for r in rows:
            h.update(r["txid"].encode())
            h.update(r["idx"].to_bytes(4, "little"))
            h.update(int(r["value"]).to_bytes(8, "little"))
            h.update(bytes.fromhex(r["pubkey_script"]))
            h.update(r["domain"].encode())
        h.update(str(len(rows)).encode())
        return h.hexdigest()


def _row_to_utxo(row: Any) -> UTXORecord:
    return UTXORecord(
        txid=row["txid"],
        idx=row["idx"],
        value=row["value"],
        pubkey_script=row["pubkey_script"],
        domain=row["domain"],
        created_run=row["created_run"],
    )
