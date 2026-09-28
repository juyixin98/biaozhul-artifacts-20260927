"""索引存储（SQLite）：只存索引与证据，不承载执行语义。

保存内容
--------
* ``tx_envelopes``：原始签名信封（回放输入，唯一可信来源）；
* ``receipts``：每次执行的收据 JSON（结果 + 状态根 + 引擎版本 + 摘要）；
* ``contracts``：地址 -> 代码，作为重建链状态的来源；
* ``snapshots``：高度 -> 状态根（供快速定位与对账）；
* ``accounts`` / ``storage``：最新余额/nonce 与存储槽（索引视图，
  可由信封全量重放重建；replay 模块就是这么校验它的）。

SQLite 写操作在事务内完成；并发以单写连接 + 外检约束保证一致性。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    height          INTEGER PRIMARY KEY,
    tx_hash         TEXT NOT NULL UNIQUE,
    status          INTEGER NOT NULL,
    halt_code       TEXT,
    gas_charged     INTEGER NOT NULL,
    pre_state_root  TEXT NOT NULL,
    post_state_root TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    result_digest   TEXT NOT NULL,
    receipt_json    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tx_envelopes (
    height    INTEGER PRIMARY KEY,
    tx_hash   TEXT NOT NULL UNIQUE,
    envelope  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
    address    TEXT PRIMARY KEY,
    height     INTEGER NOT NULL,
    code_b64   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    address  TEXT PRIMARY KEY,
    nonce    INTEGER NOT NULL,
    balance  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS storage_slots (
    address  TEXT NOT NULL,
    slot     INTEGER NOT NULL,
    value    INTEGER NOT NULL,
    PRIMARY KEY (address, slot)
);
CREATE TABLE IF NOT EXISTS snapshots (
    height      INTEGER PRIMARY KEY,
    state_root  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
-- 有序注资流水（genesis credits）：离线重放时按 seq 重放，
-- 余额不只来自“当前索引视图”，而是从这些输入事件重建。
CREATE TABLE IF NOT EXISTS credits (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    address  TEXT NOT NULL,
    amount   INTEGER NOT NULL,
    reason   TEXT NOT NULL
);
"""


class IndexStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        # 本地教学链单进程使用：允许 FastAPI 工作线程共享连接，配合 WAL 串行写。
        self.conn = sqlite3.connect(path, isolation_level=None,
                                    check_same_thread=False)
        self._writelock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._writelock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ---- 写入 ----
    def save_accepted(self, envelope: dict[str, Any], receipt: dict[str, Any]) -> None:
        height = receipt["height"]
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO tx_envelopes(height, tx_hash, envelope) "
                "VALUES (?,?,?)",
                (height, receipt["tx_hash"], json.dumps(envelope, sort_keys=True)),
            )
            c.execute(
                "INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    height, receipt["tx_hash"], receipt["status"],
                    receipt["halt_code"], receipt["gas_charged"],
                    receipt["pre_state_root"], receipt["post_state_root"],
                    receipt["engine_version"], receipt["result_digest"],
                    json.dumps(receipt, sort_keys=True),
                ),
            )
            if receipt["type"] == "deploy" and receipt["deployed_address"]:
                code_b64 = envelope["tx"]["code_b64"]
                c.execute(
                    "INSERT OR REPLACE INTO contracts(address, height, code_b64) "
                    "VALUES (?,?,?)",
                    (receipt["deployed_address"], height, code_b64),
                )
            c.execute(
                "INSERT OR REPLACE INTO snapshots(height, state_root) VALUES (?,?)",
                (height, receipt["post_state_root"]),
            )
            self._upsert_accounts_from_receipt(c, receipt)
            for addr, slot, value in receipt["writes"]:
                c.execute(
                    "INSERT OR REPLACE INTO storage_slots(address, slot, value) "
                    "VALUES (?,?,?)",
                    (addr, slot, value),
                )

    @staticmethod
    def _upsert_accounts_from_receipt(c: sqlite3.Connection, r: dict) -> None:
        # 余额的索引视图需结合扣/退：直接由调用方在外部 set_account 更稳，
        # 这里仅确保行存在；准确值由 ChainService 在结算后写入。
        c.execute(
            "INSERT OR IGNORE INTO accounts(address, nonce, balance) VALUES (?,0,0)",
            (r["from"],),
        )
        c.execute("UPDATE accounts SET nonce=? WHERE address=?",
                  (r["nonce"] + 1, r["from"]))

    def set_account(self, address: str, nonce: int, balance: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO accounts(address, nonce, balance) VALUES (?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET nonce=excluded.nonce, "
                "balance=excluded.balance",
                (address, nonce, balance),
            )

    def seed_account(self, address: str, balance: int, reason: str = "synthetic_seed") -> None:
        """记录一笔有序注资（genesis credit）并更新余额视图（单事务）。"""
        with self.tx() as c:
            c.execute(
                "INSERT INTO credits(address, amount, reason) VALUES (?,?,?)",
                (address, balance, reason),
            )
            c.execute(
                "INSERT INTO accounts(address, nonce, balance) VALUES (?,0,?) "
                "ON CONFLICT(address) DO UPDATE SET balance=balance+excluded.balance",
                (address, balance),
            )

    def credits(self) -> list[tuple[str, int, str]]:
        rows = self.conn.execute(
            "SELECT address, amount, reason FROM credits ORDER BY seq"
        ).fetchall()
        return [(r["address"], int(r["amount"]), r["reason"]) for r in rows]

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                      (key, value))

    # ---- 读取 ----
    def max_height(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(height),0) FROM receipts").fetchone()
        return int(row[0])

    def list_envelopes(self) -> list[tuple[int, dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT height, envelope FROM tx_envelopes ORDER BY height"
        ).fetchall()
        return [(r["height"], json.loads(r["envelope"])) for r in rows]

    def get_receipt(self, height: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT receipt_json FROM receipts WHERE height=?", (height,)
        ).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def get_receipt_by_tx(self, tx_hash: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT receipt_json FROM receipts WHERE tx_hash=?", (tx_hash,)
        ).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def list_receipts(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT receipt_json FROM receipts ORDER BY height DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [json.loads(r["receipt_json"]) for r in rows]

    def contracts(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT address, code_b64 FROM contracts").fetchall()
        return {r["address"]: r["code_b64"] for r in rows}

    def snapshot_roots(self) -> dict[int, str]:
        rows = self.conn.execute("SELECT height, state_root FROM snapshots").fetchall()
        return {int(r["height"]): r["state_root"] for r in rows}

    def get_account(self, address: str) -> dict[str, int] | None:
        row = self.conn.execute(
            "SELECT nonce, balance FROM accounts WHERE address=?", (address,)
        ).fetchone()
        return {"nonce": row["nonce"], "balance": row["balance"]} if row else None

    def get_slot(self, address: str, slot: int) -> int | None:
        row = self.conn.execute(
            "SELECT value FROM storage_slots WHERE address=? AND slot=?",
            (address, slot),
        ).fetchone()
        return None if row is None else int(row["value"])

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
