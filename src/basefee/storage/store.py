"""Durable index storage backed by SQLite.

Stores accepted blocks, their transactions, per-transaction receipts, invalid
(skipped) transaction records and head metadata. Big integers (wei quantities,
which may reach 2**256-1) are persisted as canonical decimal TEXT and converted
back with explicit helpers, so there is no INTEGER affinity truncation.

Writes for one block happen in a single transaction (atomic); a unique
constraint on block number guards against duplicate ingestion.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from ..params import PARAMS
from ..kernel.models import ExecutedBlock, InvalidTxRecord
from ..kernel.chain import ZERO_HASH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    number            INTEGER PRIMARY KEY,
    block_hash        TEXT NOT NULL UNIQUE,
    parent_hash       TEXT NOT NULL,
    base_fee          TEXT NOT NULL,
    gas_limit         TEXT NOT NULL,
    gas_used          TEXT NOT NULL,
    next_base_fee     TEXT NOT NULL,
    total_burned      TEXT NOT NULL,
    total_tipped      TEXT NOT NULL,
    fee_step_json     TEXT NOT NULL,
    accepted_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS transactions (
    block_number  INTEGER NOT NULL,
    tx_index      INTEGER NOT NULL,
    tx_hash       TEXT NOT NULL,
    sender        TEXT NOT NULL,
    valid         INTEGER NOT NULL,
    error_code    TEXT,
    gas           TEXT,
    tip           TEXT,
    gas_price     TEXT,
    burned        TEXT,
    total_cost    TEXT,
    PRIMARY KEY (block_number, tx_index),
    FOREIGN KEY (block_number) REFERENCES blocks(number)
);
CREATE TABLE IF NOT EXISTS invalid_transactions (
    block_number INTEGER NOT NULL,
    tx_index     INTEGER NOT NULL,
    tx_hash      TEXT NOT NULL,
    error_code   TEXT NOT NULL,
    detail       TEXT NOT NULL,
    PRIMARY KEY (block_number, tx_index)
);
CREATE TABLE IF NOT EXISTS balances (
    address       TEXT PRIMARY KEY,
    balance       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_hash ON transactions(tx_hash);
CREATE INDEX IF NOT EXISTS idx_tx_sender ON transactions(sender);
"""


class StorageError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _i(value: int) -> str:
    return str(int(value))


def _n(value: Optional[str]) -> int:
    return int(value) if value is not None else 0


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self.conn:
            self.conn.executescript(_SCHEMA)
        if self.get_meta("genesis_hash") is None:
            self.set_meta("genesis_hash", ZERO_HASH)
            self.set_meta("head_number", str(PARAMS.genesis_number))
            self.set_meta("head_hash", ZERO_HASH)
            self.set_meta("protocol_version", PARAMS.protocol_version)

    # ---------- meta ----------
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def head_number(self) -> int:
        return int(self.get_meta("head_number"))

    # ---------- writes ----------
    def save_executed(self, executed: ExecutedBlock) -> None:
        block = executed.block
        if not executed.accepted:
            raise StorageError("E044_BAD_BLOCK_HEADER", "refusing to persist rejected block")
        if self.conn.execute(
            "SELECT 1 FROM blocks WHERE number=?", (block.number,)
        ).fetchone():
            raise StorageError("E050_BLOCK_EXISTS",
                               f"block {block.number} already stored")
        if block.number != self.head_number() + 1:
            raise StorageError("E052_REPLAY_GAP",
                               f"block {block.number} does not extend head {self.head_number()}")
        expected_parent = self.get_meta("head_hash")
        if block.parent_hash != expected_parent:
            raise StorageError("E051_PARENT_UNKNOWN",
                               f"parent_hash {block.parent_hash} != head {expected_parent}")

        with self.conn:
            self.conn.execute(
                "INSERT INTO blocks(number, block_hash, parent_hash, base_fee, gas_limit, "
                "gas_used, next_base_fee, total_burned, total_tipped, fee_step_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (_i(block.number), block.block_hash, block.parent_hash,
                 _i(block.base_fee_per_gas), _i(block.gas_limit), _i(block.gas_used),
                 _i(executed.next_base_fee), _i(executed.total_burned),
                 _i(executed.total_tipped), json.dumps(executed.fee_step_explanation)),
            )
            for idx, r in enumerate(executed.receipts):
                self.conn.execute(
                    "INSERT INTO transactions(block_number, tx_index, tx_hash, sender, "
                    "valid, error_code, gas, tip, gas_price, burned, total_cost) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (block.number, idx, r.tx_hash, r.sender, 1 if r.valid else 0,
                     r.error_code, _i(r.gas), _i(r.effective_priority_tip),
                     _i(r.effective_gas_price), _i(r.burned), _i(r.total_cost)),
                )
            rec: InvalidTxRecord
            for rec in executed.invalid:
                self.conn.execute(
                    "INSERT INTO invalid_transactions(block_number, tx_index, tx_hash, "
                    "error_code, detail) VALUES(?,?,?,?,?)",
                    (block.number, rec.index, rec.tx_hash, rec.error_code, rec.detail),
                )
            for addr, bal in executed.balances_after.items():
                self.conn.execute(
                    "INSERT INTO balances(address, balance) VALUES(?, ?) "
                    "ON CONFLICT(address) DO UPDATE SET balance=excluded.balance",
                    (addr, _i(bal)),
                )
            self.set_meta("head_number", str(block.number))
            self.set_meta("head_hash", block.block_hash)

    # ---------- reads ----------
    def get_block_row(self, number: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM blocks WHERE number=?", (number,)
        ).fetchone()

    def get_block_by_hash(self, block_hash: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM blocks WHERE block_hash=?", (block_hash,)
        ).fetchone()

    def get_transactions(self, number: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM transactions WHERE block_number=? ORDER BY tx_index",
            (number,),
        ))

    def get_invalid(self, number: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM invalid_transactions WHERE block_number=? ORDER BY tx_index",
            (number,),
        ))

    def get_balance(self, address: str) -> int:
        row = self.conn.execute(
            "SELECT balance FROM balances WHERE address=?", (address,)
        ).fetchone()
        return int(row["balance"]) if row else 0

    def list_blocks(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM blocks ORDER BY number"))

    def close(self) -> None:
        self.conn.close()
