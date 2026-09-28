"""Indexed storage for applied blocks and receipts (SQLite).

The kernel (:mod:`basefee_model.core`) is the source of truth during a run and
is pure/in-memory; this module persists its results into a durable, indexed
SQLite store so blocks and transactions can be queried offline and a replay
can be verified.

Integer handling
----------------
Gas quantities, block numbers and nonces fit in 64 bits and are stored as
SQLite ``INTEGER``. **Wei amounts, however, are EVM ``uint256`` values that can
far exceed SQLite's signed-64-bit ``INTEGER`` range**, so every money column is
stored as a canonical hex string (``"0x.."``) and parsed back to ``int``.
Aggregations over money are therefore performed in Python, never ``SUM()``.

All writes for one block happen in a single transaction so a failed block never
leaves partial rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..core.state import BlockResult, ChainState

_MONEY_COLS_BLOCK = ("base_fee", "next_base_fee", "burned", "tips",
                     "transferred", "total_debited")
_MONEY_COLS_TX = ("effective_gas_price", "base_fee_charged", "tip_charged",
                  "burned", "value", "sender_debit")


def _h(value: int) -> str:
    return hex(int(value))


def _i(value: str) -> int:
    return int(value, 16)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    number INTEGER PRIMARY KEY,
    hash TEXT UNIQUE NOT NULL,
    parent_hash TEXT NOT NULL,
    base_fee TEXT NOT NULL,
    next_base_fee TEXT NOT NULL,
    gas_limit INTEGER NOT NULL,
    gas_used INTEGER NOT NULL,
    gas_target INTEGER NOT NULL,
    burned TEXT NOT NULL,
    tips TEXT NOT NULL,
    transferred TEXT NOT NULL,
    total_debited TEXT NOT NULL,
    tx_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    tx_hash TEXT PRIMARY KEY,
    block_number INTEGER NOT NULL,
    tx_index INTEGER NOT NULL,
    sender TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    tx_type INTEGER NOT NULL,
    nonce INTEGER NOT NULL,
    gas_used INTEGER NOT NULL,
    effective_gas_price TEXT NOT NULL,
    base_fee_charged TEXT NOT NULL,
    tip_charged TEXT NOT NULL,
    burned TEXT NOT NULL,
    value TEXT NOT NULL,
    sender_debit TEXT NOT NULL,
    raw_tx BLOB NOT NULL,
    FOREIGN KEY(block_number) REFERENCES blocks(number)
);
CREATE INDEX IF NOT EXISTS ix_tx_block ON transactions(block_number);
CREATE INDEX IF NOT EXISTS ix_tx_sender ON transactions(sender);
"""


class IndexStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta --------------------------------------------------------------
    def set_meta(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # -- writes ------------------------------------------------------------
    def block_exists(self, number: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM blocks WHERE number=?", (number,)).fetchone()
        return row is not None

    def save_block_result(self, state: ChainState, result: BlockResult) -> None:
        block = state.blocks[result.number]
        created = datetime.now(timezone.utc).isoformat()
        try:
            with self.conn:  # atomic per block
                self.conn.execute(
                    "INSERT INTO blocks(number, hash, parent_hash, base_fee, "
                    "next_base_fee, gas_limit, gas_used, gas_target, burned, "
                    "tips, transferred, total_debited, tx_count, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (result.number, result.hash.hex(),
                     block.parent_hash.hex(), _h(result.base_fee),
                     _h(result.next_base_fee), result.gas_limit,
                     result.gas_used, result.gas_target, _h(result.burned),
                     _h(result.tips), _h(result.transferred),
                     _h(result.total_debited), len(result.receipts), created),
                )
                for idx, rcpt in enumerate(result.receipts):
                    tx = block.transactions[idx]
                    self.conn.execute(
                        "INSERT INTO transactions(tx_hash, block_number, "
                        "tx_index, sender, to_addr, tx_type, nonce, gas_used, "
                        "effective_gas_price, base_fee_charged, tip_charged, "
                        "burned, value, sender_debit, raw_tx) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rcpt.tx_hash.hex(), result.number, idx,
                         rcpt.sender.hex(), rcpt.to.hex(), tx.type, tx.nonce,
                         rcpt.gas_used, _h(rcpt.effective_gas_price),
                         _h(rcpt.base_fee_charged), _h(rcpt.tip_charged),
                         _h(rcpt.burned), _h(rcpt.value), _h(rcpt.sender_debit),
                         tx.encoded()),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"block {result.number} already stored: {exc}") \
                from exc

    # -- decoding ----------------------------------------------------------
    @staticmethod
    def _decode_block(row: sqlite3.Row) -> dict:
        d = dict(row)
        for col in _MONEY_COLS_BLOCK:
            d[col] = _i(d[col])
        return d

    @staticmethod
    def _decode_tx(row: sqlite3.Row) -> dict:
        d = dict(row)
        for col in _MONEY_COLS_TX:
            d[col] = _i(d[col])
        return d

    # -- reads -------------------------------------------------------------
    def latest_number(self) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(number) AS m FROM blocks").fetchone()
        return row["m"] if row and row["m"] is not None else None

    def get_block(self, number: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM blocks WHERE number=?", (number,)).fetchone()
        return self._decode_block(row) if row else None

    def list_blocks(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM blocks ORDER BY number").fetchall()
        return [self._decode_block(r) for r in rows]

    def get_transactions(self, block_number: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE block_number=? ORDER BY tx_index",
            (block_number,)).fetchall()
        return [self._decode_tx(r) for r in rows]

    def get_transaction(self, tx_hash_hex: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM transactions WHERE tx_hash=?",
            (tx_hash_hex.lstrip("0x"),)).fetchone()
        return self._decode_tx(row) if row else None

    def get_transactions_by_sender(self, address_hex: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE sender=? "
            "ORDER BY block_number, tx_index",
            (address_hex.lower().lstrip("0x"),)).fetchall()
        return [self._decode_tx(r) for r in rows]

    def base_fee_timeline(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT number, base_fee, next_base_fee, gas_used, gas_limit, "
            "gas_target FROM blocks ORDER BY number").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["base_fee"] = _i(d["base_fee"])
            d["next_base_fee"] = _i(d["next_base_fee"])
            out.append(d)
        return out

    def totals(self) -> dict:
        # Money is uint256 hex; aggregate in Python where big ints are native.
        rows = self.conn.execute(
            "SELECT burned, tips, transferred, total_debited "
            "FROM blocks").fetchall()
        burned = tips = transferred = debited = 0
        for r in rows:
            burned += _i(r["burned"])
            tips += _i(r["tips"])
            transferred += _i(r["transferred"])
            debited += _i(r["total_debited"])
        return {
            "blocks": len(rows),
            "total_burned": burned,
            "total_tips": tips,
            "total_transferred": transferred,
            "total_debited": debited,
            "conserved": debited == burned + tips + transferred,
            "difference": debited - (burned + tips + transferred),
        }
