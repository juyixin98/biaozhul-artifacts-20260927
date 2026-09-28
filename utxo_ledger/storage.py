"""SQLite indexed storage for UTXOs, blocks, transactions and spend history.

Implements the :class:`~utxo_ledger.kernel.ChainView` protocol so the kernel can
read committed state directly. Writes happen through exactly one method --
:meth:`SqliteStore.apply_block` -- which applies a validated
:class:`BlockEffect` inside a single ``BEGIN IMMEDIATE`` transaction. Any error
inside that transaction rolls the whole thing back, which is the storage-side
guarantee behind "an invalid block commits nothing".

Tables
------
* ``meta``         -- singleton chain tip (height, hash).
* ``blocks``       -- one row per committed block, raw bytes retained for replay.
* ``transactions`` -- txid -> block/position, raw bytes and fee.
* ``utxos``        -- live output set (the index the kernel consults).
* ``spent``        -- append-only spend history (where/when an outpoint went).

All ``sqlite3`` errors are translated to :class:`LedgerError`
(``STORAGE_FAILURE`` -- COMPUTATION) except integrity conflicts that have a
chain meaning (duplicate block/txid), which keep their STATE codes.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .encoding import Outpoint, encode_block, encode_tx
from .errors import ErrorCode, LedgerError
from .kernel import SPENT, BlockEffect, Utxo
from .protocol import GENESIS_HEIGHT, ZERO_HASH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tip_height INTEGER NOT NULL,
    tip_hash   BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    height     INTEGER PRIMARY KEY,
    hash       BLOB NOT NULL UNIQUE,
    prev_hash  BLOB NOT NULL,
    subsidy    INTEGER NOT NULL,
    fee_total  INTEGER NOT NULL,
    raw        BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    txid         BLOB PRIMARY KEY,
    block_height INTEGER NOT NULL REFERENCES blocks(height),
    position     INTEGER NOT NULL,
    fee          INTEGER NOT NULL,
    is_coinbase  INTEGER NOT NULL,
    raw          BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_height, position);

CREATE TABLE IF NOT EXISTS utxos (
    txid           BLOB NOT NULL,
    vout           INTEGER NOT NULL,
    value          INTEGER NOT NULL,
    pubkey         BLOB NOT NULL,
    created_height INTEGER NOT NULL,
    PRIMARY KEY (txid, vout)
);
CREATE INDEX IF NOT EXISTS idx_utxo_pubkey ON utxos(pubkey);

CREATE TABLE IF NOT EXISTS spent (
    txid        BLOB NOT NULL,
    vout        INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    spent_txid  BLOB NOT NULL,
    spent_height INTEGER NOT NULL,
    PRIMARY KEY (txid, vout)
);
CREATE INDEX IF NOT EXISTS idx_spent_tx ON spent(spent_txid);
"""


class SqliteStore:
    def __init__(self, path: str = ":memory:") -> None:
        try:
            # check_same_thread=False: the HTTP boundary serves requests from a
            # worker thread; BEGIN IMMEDIATE still serializes all writers.
            self._conn = sqlite3.connect(
                path, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise LedgerError(
                ErrorCode.STORAGE_FAILURE, f"cannot open database {path!r}: {exc!r}"
            ) from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self.initialize()
        except sqlite3.Error as exc:
            raise LedgerError(
                ErrorCode.STORAGE_FAILURE, f"schema initialization failed: {exc!r}"
            ) from exc

    # -- lifecycle -----------------------------------------------------------
    def initialize(self) -> None:
        # executescript() manages its own transaction; keep it out of _tx().
        cur = self._conn.cursor()
        try:
            cur.executescript(_SCHEMA)
            row = cur.execute("SELECT 1 FROM meta WHERE id = 1").fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO meta (id, tip_height, tip_hash) VALUES (1, ?, ?)",
                    (GENESIS_HEIGHT, ZERO_HASH),
                )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise LedgerError(
                ErrorCode.STORAGE_FAILURE, f"close failed: {exc!r}"
            ) from exc

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            yield cur
            cur.execute("COMMIT")
        except Exception:
            # executescript() and similar calls can end the transaction
            # implicitly; ROLLBACK only makes sense while one is active.
            if self._conn.in_transaction:
                cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()

    # -- ChainView -----------------------------------------------------------
    @property
    def tip_height(self) -> int:
        return self._scalar("SELECT tip_height FROM meta WHERE id = 1")

    @property
    def tip_hash(self) -> bytes:
        return self._scalar("SELECT tip_hash FROM meta WHERE id = 1")

    def lookup_committed(self, outpoint: Outpoint):
        row = self._query_one(
            "SELECT value, pubkey, created_height FROM utxos "
            "WHERE txid = ? AND vout = ?",
            (outpoint.txid, outpoint.vout),
        )
        if row is not None:
            return Utxo(row["value"], row["pubkey"], row["created_height"])
        spent = self._query_one(
            "SELECT 1 FROM spent WHERE txid = ? AND vout = ?",
            (outpoint.txid, outpoint.vout),
        )
        return SPENT if spent is not None else None

    # -- writes (single atomic unit) -----------------------------------------
    def apply_block(self, effect: BlockEffect) -> None:
        block = effect.block
        raw_block = encode_block(block)
        try:
            with self._tx() as cur:
                # Re-check the tip inside the write transaction: another writer
                # must not have advanced the chain between validate and commit.
                row = cur.execute(
                    "SELECT tip_height, tip_hash FROM meta WHERE id = 1"
                ).fetchone()
                if row["tip_height"] != effect.height - 1 or row["tip_hash"] != effect.prev_hash:
                    raise LedgerError(
                        ErrorCode.PREV_BLOCK_HASH_MISMATCH,
                        "chain tip moved between validation and commit",
                    )
                try:
                    cur.execute(
                        "INSERT INTO blocks (height, hash, prev_hash, subsidy, "
                        "fee_total, raw) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            effect.height,
                            effect.block_hash,
                            effect.prev_hash,
                            effect.subsidy,
                            effect.fees_total,
                            raw_block,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise LedgerError(
                        ErrorCode.DUPLICATE_BLOCK,
                        f"block height {effect.height} or hash "
                        f"{effect.block_hash.hex()} already committed: {exc!r}",
                    ) from exc

                for te in effect.tx_effects:
                    raw_tx = encode_tx(te.tx)
                    try:
                        cur.execute(
                            "INSERT INTO transactions (txid, block_height, "
                            "position, fee, is_coinbase, raw) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                te.txid,
                                effect.height,
                                te.index,
                                te.fee,
                                1 if te.index == 0 else 0,
                                raw_tx,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise LedgerError(
                            ErrorCode.DUPLICATE_TXID,
                            f"txid {te.txid.hex()} already exists: {exc!r}",
                            tx_index=te.index,
                        ) from exc

                for te in effect.tx_effects:
                    for op in te.spent:
                        row = cur.execute(
                            "SELECT value FROM utxos WHERE txid = ? AND vout = ?",
                            (op.txid, op.vout),
                        ).fetchone()
                        if row is None:
                            # Cannot happen after a successful kernel validation
                            # unless storage was corrupted externally.
                            raise LedgerError(
                                ErrorCode.STORAGE_FAILURE,
                                "commit invariant violated: spending missing utxo "
                                f"{op.txid.hex()}:{op.vout}",
                                tx_index=te.index,
                            )
                        cur.execute(
                            "DELETE FROM utxos WHERE txid = ? AND vout = ?",
                            (op.txid, op.vout),
                        )
                        cur.execute(
                            "INSERT INTO spent (txid, vout, value, spent_txid, "
                            "spent_height) VALUES (?, ?, ?, ?, ?)",
                            (op.txid, op.vout, row["value"], te.txid, effect.height),
                        )
                    for op, utxo in te.created:
                        cur.execute(
                            "INSERT INTO utxos (txid, vout, value, pubkey, "
                            "created_height) VALUES (?, ?, ?, ?, ?)",
                            (op.txid, op.vout, utxo.value, utxo.pubkey, utxo.created_height),
                        )

                cur.execute(
                    "UPDATE meta SET tip_height = ?, tip_hash = ? WHERE id = 1",
                    (effect.height, effect.block_hash),
                )
        except sqlite3.Error as exc:
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError(
                ErrorCode.STORAGE_FAILURE, f"database write failed: {exc!r}"
            ) from exc

    # -- read model -----------------------------------------------------------
    def get_block_raw(self, height: int) -> bytes | None:
        row = self._query_one("SELECT raw FROM blocks WHERE height = ?", (height,))
        return row["raw"] if row else None

    def get_block_info(self, height: int) -> dict | None:
        row = self._query_one(
            "SELECT height, hash, prev_hash, subsidy, fee_total FROM blocks "
            "WHERE height = ?",
            (height,),
        )
        if row is None:
            return None
        return {
            "height": row["height"],
            "hash": row["hash"].hex(),
            "prev_hash": row["prev_hash"].hex(),
            "subsidy": row["subsidy"],
            "fee_total": row["fee_total"],
        }

    def get_tx_raw(self, txid: bytes) -> bytes | None:
        row = self._query_one(
            "SELECT raw FROM transactions WHERE txid = ?", (txid,)
        )
        return row["raw"] if row else None

    def get_tx_info(self, txid: bytes) -> dict | None:
        row = self._query_one(
            "SELECT txid, block_height, position, fee, is_coinbase, raw "
            "FROM transactions WHERE txid = ?",
            (txid,),
        )
        if row is None:
            return None
        return {
            "txid": row["txid"].hex(),
            "block_height": row["block_height"],
            "position": row["position"],
            "fee": row["fee"],
            "is_coinbase": bool(row["is_coinbase"]),
        }

    def get_utxo(self, txid: bytes, vout: int) -> dict | None:
        row = self._query_one(
            "SELECT value, pubkey, created_height FROM utxos "
            "WHERE txid = ? AND vout = ?",
            (txid, vout),
        )
        if row is None:
            return None
        return {
            "txid": txid.hex(),
            "vout": vout,
            "value": row["value"],
            "pubkey": row["pubkey"].hex(),
            "created_height": row["created_height"],
        }

    def list_utxos(self, pubkey: bytes | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(limit, 1000))
        if pubkey is not None:
            rows = self._conn.execute(
                "SELECT txid, vout, value, pubkey, created_height FROM utxos "
                "WHERE pubkey = ? ORDER BY txid, vout LIMIT ?",
                (pubkey, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT txid, vout, value, pubkey, created_height FROM utxos "
                "ORDER BY txid, vout LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "txid": r["txid"].hex(),
                "vout": r["vout"],
                "value": r["value"],
                "pubkey": r["pubkey"].hex(),
                "created_height": r["created_height"],
            }
            for r in rows
        ]

    def utxo_count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM utxos")

    def balance(self, pubkey: bytes) -> int:
        return self._scalar(
            "SELECT COALESCE(SUM(value), 0) FROM utxos WHERE pubkey = ?",
            (pubkey,),
        )

    def snapshot_utxos(self) -> dict[tuple[str, int], dict]:
        rows = self._conn.execute(
            "SELECT txid, vout, value, pubkey, created_height FROM utxos "
            "ORDER BY txid, vout"
        ).fetchall()
        return {
            (r["txid"].hex(), r["vout"]): {
                "value": r["value"],
                "pubkey": r["pubkey"].hex(),
                "height": r["created_height"],
            }
            for r in rows
        }

    def spend_history(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT txid, vout, value, spent_txid, spent_height FROM spent "
            "ORDER BY spent_height DESC, spent_txid LIMIT ?",
            (max(1, min(limit, 10_000)),),
        ).fetchall()
        return [
            {
                "outpoint": {"txid": r["txid"].hex(), "vout": r["vout"]},
                "value": r["value"],
                "spent_txid": r["spent_txid"].hex(),
                "spent_height": r["spent_height"],
            }
            for r in rows
        ]

    # -- small internal helpers ----------------------------------------------
    def _query_one(self, sql: str, params: tuple):
        try:
            return self._conn.execute(sql, params).fetchone()
        except sqlite3.Error as exc:
            raise LedgerError(
                ErrorCode.STORAGE_FAILURE, f"query failed: {exc!r}"
            ) from exc

    def _scalar(self, sql: str, params: tuple = ()):
        try:
            return self._conn.execute(sql, params).fetchone()[0]
        except sqlite3.Error as exc:
            raise LedgerError(
                ErrorCode.STORAGE_FAILURE, f"query failed: {exc!r}"
            ) from exc
