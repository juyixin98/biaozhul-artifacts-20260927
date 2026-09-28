"""Indexed storage over SQLite.

Durable, queryable index of blocks/transactions/receipts/events/accounts plus a
replay-runs table. All writes are parameterised and happen inside one
transaction per block. Correlation/run ids are stored on every row so a log
line or report can be traced to the run that produced it.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from ..chain import ChainState, Receipt, Transaction
from ..log_utils import get_logger

log = get_logger("storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    mode          TEXT NOT NULL,
    tx_count      INTEGER NOT NULL DEFAULT 0,
    ok_count      INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS blocks (
    block_number  INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL,
    state_root    TEXT NOT NULL,
    tx_count      INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_index      INTEGER NOT NULL,
    block_number  INTEGER NOT NULL,
    run_id        TEXT NOT NULL,
    sender        TEXT NOT NULL,
    nonce         INTEGER NOT NULL,
    call          TEXT NOT NULL,
    calldata      TEXT NOT NULL,
    signature     TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    error_code    TEXT,
    error_message TEXT,
    PRIMARY KEY (block_number, tx_index)
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    block_number  INTEGER NOT NULL,
    tx_index      INTEGER NOT NULL,
    name          TEXT NOT NULL,
    args_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    address       TEXT PRIMARY KEY,
    balance       TEXT NOT NULL,
    nonce         INTEGER NOT NULL,
    allowances_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_run ON transactions(run_id);
CREATE INDEX IF NOT EXISTS idx_ev_block ON events(block_number, tx_index);
"""


class Repository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- runs ------------------------------------------------------------ #
    def start_run(self, run_id: str, started_at: str, mode: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO replay_runs(run_id,started_at,mode,status)"
                " VALUES (?,?,?, 'running')",
                (run_id, started_at, mode),
            )

    def finish_run(
        self, run_id: str, finished_at: str, tx_count: int, ok: int, fail: int,
        status: str, detail: str,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE replay_runs SET finished_at=?, tx_count=?, ok_count=?,"
                " fail_count=?, status=?, detail=? WHERE run_id=?",
                (finished_at, tx_count, ok, fail, status, detail, run_id),
            )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM replay_runs WHERE run_id=?", (run_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM replay_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    # ---- blocks / txs ---------------------------------------------------- #
    def next_block_number(self) -> int:
        cur = self._conn.execute("SELECT COALESCE(MAX(block_number), -1) + 1 AS n FROM blocks")
        return int(cur.fetchone()["n"])

    def save_block(
        self, block_number: int, run_id: str, state_root: bytes,
        records: List[Dict[str, Any]],
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO blocks(block_number,run_id,state_root,tx_count)"
                " VALUES (?,?,?,?)",
                (block_number, run_id, state_root.hex(), len(records)),
            )
            for tx_index, rec in enumerate(records):
                t: Transaction = rec["transaction"]
                c.execute(
                    "INSERT INTO transactions(block_number,tx_index,run_id,sender,nonce,"
                    "call,calldata,signature,ok,error_code,error_message)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        block_number,
                        tx_index,
                        run_id,
                        hex(t.sender),
                        t.nonce,
                        rec.get("call", ""),
                        t.calldata.hex(),
                        t.signature.to_bytes().hex(),
                        1 if rec["ok"] else 0,
                        rec.get("error_code"),
                        rec.get("error_message"),
                    ),
                )
                for ev in rec.get("events", []):
                    c.execute(
                        "INSERT INTO events(block_number,tx_index,name,args_json)"
                        " VALUES (?,?,?,?)",
                        (block_number, tx_index, ev.name, json.dumps(ev.args, sort_keys=True)),
                    )

    def save_accounts(self, state: ChainState) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM accounts")
            for addr in sorted(state.accounts):
                a = state.accounts[addr]
                c.execute(
                    "INSERT INTO accounts(address,balance,nonce,allowances_json)"
                    " VALUES (?,?,?,?)",
                    (
                        hex(addr),
                        str(a.balance),
                        a.nonce,
                        json.dumps({hex(k): str(v) for k, v in a.allowances.items()}, sort_keys=True),
                    ),
                )

    # ---- queries --------------------------------------------------------- #
    def list_transactions(self, run_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if run_id:
            cur = self._conn.execute(
                "SELECT * FROM transactions WHERE run_id=? ORDER BY block_number,tx_index LIMIT ?",
                (run_id, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM transactions ORDER BY block_number,tx_index LIMIT ?", (limit,)
            )
        return [dict(r) for r in cur.fetchall()]

    def get_account(self, address: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM accounts WHERE lower(address)=lower(?)", (address,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> List[Dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM accounts ORDER BY address")
        return [dict(r) for r in cur.fetchall()]

    def list_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM events ORDER BY block_number,tx_index,id LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    def last_block(self) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM blocks ORDER BY block_number DESC LIMIT 1")
        row = cur.fetchone()
        return dict(row) if row else None

    def reset(self) -> None:
        with self.tx() as c:
            for table in ("events", "transactions", "blocks", "accounts", "replay_runs"):
                c.execute(f"DELETE FROM {table}")
        log.info("storage reset")
