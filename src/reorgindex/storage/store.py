"""SQLite-backed derived-index store.

Responsibilities (storage only -- no consensus rules):

* block bookkeeping: every sealed-valid block, active-chain membership, and
  the orphan suspension table;
* the **revocable derived index** (``derived_events``) and its materialized
  account view (``account_state``);
* the durable two-phase ``switch_plan`` used by the chain kernel;
* structured diagnostics rows.

A fork switch runs inside SQLite transactions.  Readers therefore observe
either the complete old chain version or the complete new one -- never a mix.
The plan is committed after the detach phase so an interrupted process can
resume exactly once (see ``resume_switch``).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .schema import SCHEMA_SQL


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SwitchInterrupted(RuntimeError):
    """Raised by the test crash-injection point after the detach commit."""


class IndexStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # explicit BEGIN/COMMIT
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    # ------------------------------------------------------------------
    def _initialize(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute(
            "INSERT OR IGNORE INTO counters(name, value) VALUES ('arrival', 0)"
        )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------- counters
    def next_arrival_seq(self) -> int:
        with self._lock:
            self.conn.execute(
                "UPDATE counters SET value = value + 1 WHERE name = 'arrival'"
            )
            row = self.conn.execute(
                "SELECT value FROM counters WHERE name = 'arrival'"
            ).fetchone()
            return int(row["value"])

    # ----------------------------------------------------------- blocks
    @staticmethod
    def _payload(block: dict) -> str:
        return json.dumps(block, sort_keys=True, separators=(",", ":"))

    def has_block(self, block_hash: str) -> bool:
        with self._lock:
            return (
                self.conn.execute(
                    "SELECT 1 FROM blocks WHERE hash = ?", (block_hash,)
                ).fetchone()
                is not None
            )

    def get_block_payload(self, block_hash: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT payload_json FROM blocks WHERE hash = ?", (block_hash,)
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def get_block_meta(self, block_hash: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT hash, height, parent, weight, difficulty, producer, "
                "timestamp, is_active FROM blocks WHERE hash = ?",
                (block_hash,),
            ).fetchone()

    def iter_blocks(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT hash, height, parent, weight, difficulty, producer, "
                    "timestamp, is_active, payload_json FROM blocks "
                    "ORDER BY first_seen_seq"
                )
            )

    def insert_block(
        self,
        *,
        block_hash: str,
        block: dict,
        weight: int,
        is_active: bool,
        seq: Optional[int] = None,
    ) -> int:
        seq = seq if seq is not None else self.next_arrival_seq()
        with self._lock:
            self.conn.execute(
                "INSERT INTO blocks(hash, height, parent, weight, difficulty, "
                "producer, timestamp, payload_json, first_seen_seq, is_active) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    block_hash,
                    block["height"],
                    block["parent"],
                    weight,
                    block["difficulty"],
                    block["producer"],
                    block["timestamp"],
                    self._payload(block),
                    seq,
                    1 if is_active else 0,
                ),
            )
        return seq

    # ----------------------------------------------------- active chain
    def active_height_hash_map(self) -> dict[int, str]:
        with self._lock:
            return {
                int(r["height"]): r["block_hash"]
                for r in self.conn.execute(
                    "SELECT height, block_hash FROM active_chain"
                )
            }

    def active_hashes(self) -> list[str]:
        with self._lock:
            return [
                r["block_hash"]
                for r in self.conn.execute(
                    "SELECT block_hash FROM active_chain ORDER BY height"
                )
            ]

    def active_tip(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT b.* FROM active_chain a JOIN blocks b ON b.hash = a.block_hash "
                "ORDER BY a.height DESC LIMIT 1"
            ).fetchone()

    def active_block_at_height(self, height: int) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT block_hash FROM active_chain WHERE height = ?", (height,)
            ).fetchone()
            return row["block_hash"] if row else None

    def is_active_hash(self, block_hash: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT is_active FROM blocks WHERE hash = ?", (block_hash,)
            ).fetchone()
            return bool(row and row["is_active"])

    # ---------------------------------------------------------- orphans
    def put_pending(
        self, block_hash: str, block: dict, seq: Optional[int] = None
    ) -> int:
        seq = seq if seq is not None else self.next_arrival_seq()
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO pending_blocks"
                "(hash, parent, payload_json, arrived_seq) VALUES (?,?,?,?)",
                (block_hash, block["parent"], self._payload(block), seq),
            )
        return seq

    def pop_pending_children(
        self, parent_hash: str
    ) -> list[tuple[str, dict, int]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT hash, payload_json, arrived_seq FROM pending_blocks "
                "WHERE parent = ? ORDER BY arrived_seq",
                (parent_hash,),
            ).fetchall()
            if rows:
                self.conn.execute(
                    "DELETE FROM pending_blocks WHERE parent = ?", (parent_hash,)
                )
            return [
                (r["hash"], json.loads(r["payload_json"]), int(r["arrived_seq"]))
                for r in rows
            ]

    def delete_pending(self, block_hash: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM pending_blocks WHERE hash = ?", (block_hash,)
            )

    def pending_count(self) -> int:
        with self._lock:
            return int(
                self.conn.execute(
                    "SELECT COUNT(*) c FROM pending_blocks"
                ).fetchone()["c"]
            )

    def all_pending(self) -> list[tuple[str, dict, int]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT hash, payload_json, arrived_seq FROM pending_blocks "
                "ORDER BY arrived_seq"
            ).fetchall()
            return [
                (r["hash"], json.loads(r["payload_json"]), int(r["arrived_seq"]))
                for r in rows
            ]

    # ----------------------------------------------------------- switch
    def save_plan(
        self,
        *,
        new_tip: str,
        detach: list[str],
        attach: list[str],
        phase: str,
        request_id: str,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO switch_plan(id, new_tip, "
                "detach_hashes_json, attach_hashes_json, phase, request_id) "
                "VALUES (1,?,?,?,?,?)",
                (
                    new_tip,
                    json.dumps(detach),
                    json.dumps(attach),
                    phase,
                    request_id,
                ),
            )

    def get_plan(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM switch_plan WHERE id = 1"
            ).fetchone()

    def clear_plan(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM switch_plan WHERE id = 1")

    # ----------------------------------------------------- delta engine
    def _account_delta(self, address: str, amount_delta: int, nonce_delta: int) -> None:
        if address is None:
            return
        self.conn.execute(
            "INSERT INTO account_state(address, balance, nonce) VALUES (?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET "
            "balance = balance + excluded.balance, nonce = nonce + excluded.nonce",
            (address, amount_delta, nonce_delta),
        )

    def _insert_events(self, block_hash: str, height: int, deltas: list[dict]) -> None:
        self.conn.executemany(
            "INSERT INTO derived_events(block_hash, height, txid, position_in_block, "
            "kind, address, amount_delta, nonce_delta) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    block_hash,
                    height,
                    d["txid"],
                    d["position_in_block"],
                    d["kind"],
                    d["address"],
                    d["amount_delta"],
                    d["nonce_delta"],
                )
                for d in deltas
            ],
        )

    def _events_for_block(self, block_hash: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT kind, address, amount_delta, nonce_delta, position_in_block "
                "FROM derived_events WHERE block_hash = ? ORDER BY position_in_block",
                (block_hash,),
            )
        )

    def _rollback_block_accounts(self, block_hash: str) -> None:
        """Undo one block's contribution to the materialized account view.

        Events are applied forward per block; reversal walks positions in
        reverse and negates each delta (commutative here, but reverse order is
        kept so a future non-commutative derivation stays correct).
        """
        rows = self._events_for_block(block_hash)
        for row in reversed(rows):
            self._account_delta(
                row["address"], -int(row["amount_delta"]), -int(row["nonce_delta"])
            )

    def _apply_block_accounts(self, deltas: list[dict]) -> None:
        for d in deltas:
            self._account_delta(d["address"], d["amount_delta"], d["nonce_delta"])

    # ------------------------------------------------- linear extension
    def apply_extension(
        self, *, block_hash: str, height: int, deltas: list[dict], weight: int
    ) -> None:
        """Append one block to the active chain (already consensus-valid)."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE blocks SET is_active = 1 WHERE hash = ?", (block_hash,)
                )
                self.conn.execute(
                    "INSERT INTO active_chain(height, block_hash) VALUES (?, ?)",
                    (height, block_hash),
                )
                self._insert_events(block_hash, height, deltas)
                self._apply_block_accounts(deltas)
                for d in deltas:
                    self.conn.execute(
                        "INSERT INTO tx_locations(txid, block_hash, height, on_active) "
                        "VALUES (?,?,?,1) ON CONFLICT(txid, block_hash) DO UPDATE SET on_active=1",
                        (d["txid"], block_hash, height),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def insert_fork_locations(self, block_hash: str, height: int, txids: list[str]) -> None:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO tx_locations(txid, block_hash, height, on_active) "
                    "VALUES (?,?,?,0)",
                    [(t, block_hash, height) for t in txids],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------- switch
    def begin_detach(
        self,
        *,
        new_tip: str,
        detach: list[str],
        attach: list[str],
        request_id: str,
    ) -> None:
        """Phase 1: persist plan, roll back old suffix, commit at DETACHED."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.save_plan(
                    new_tip=new_tip,
                    detach=detach,
                    attach=attach,
                    phase="DETACHED",
                    request_id=request_id,
                )
                for h in reversed(detach):
                    self._rollback_block_accounts(h)
                    self.conn.execute(
                        "DELETE FROM derived_events WHERE block_hash = ?", (h,)
                    )
                    self.conn.execute(
                        "DELETE FROM active_chain WHERE block_hash = ?", (h,)
                    )
                    self.conn.execute(
                        "UPDATE tx_locations SET on_active = 0 WHERE block_hash = ?",
                        (h,),
                    )
                    self.conn.execute(
                        "UPDATE blocks SET is_active = 0 WHERE hash = ?", (h,)
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def finish_attach(
        self,
        *,
        attach: list[dict],
        deltas_by_hash: dict[str, list[dict]],
    ) -> None:
        """Phase 2: apply the new suffix and clear the plan."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for meta in attach:
                    h = meta["hash"]
                    height = int(meta["height"])
                    deltas = deltas_by_hash[h]
                    self.conn.execute(
                        "UPDATE blocks SET is_active = 1 WHERE hash = ?", (h,)
                    )
                    self.conn.execute(
                        "INSERT INTO active_chain(height, block_hash) VALUES (?, ?)",
                        (height, h),
                    )
                    self._insert_events(h, height, deltas)
                    self._apply_block_accounts(deltas)
                    self.conn.execute(
                        "UPDATE tx_locations SET on_active = 1 WHERE block_hash = ?",
                        (h,),
                    )
                self.clear_plan()
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def apply_switch(
        self,
        *,
        detach: list[str],
        attach: list[dict],
        deltas_by_hash: dict[str, list[dict]],
        request_id: str,
        new_tip: str,
        crash_point: Optional[str] = None,
    ) -> dict:
        """Detach the old suffix then attach the new suffix (two committed
        phases sharing one durable plan).

        ``crash_point="after_detach"`` is the test hook: phase 1 commits and
        the process "dies" before phase 2, leaving a DETACHED plan that
        ``resume_switch`` completes.
        """
        self.begin_detach(
            new_tip=new_tip,
            detach=detach,
            attach=[b["hash"] for b in attach],
            request_id=request_id,
        )
        if crash_point == "after_detach":
            raise SwitchInterrupted(
                "simulated crash after detach commit (plan persisted, phase=DETACHED)"
            )
        self.finish_attach(attach=attach, deltas_by_hash=deltas_by_hash)
        return {
            "detached": detach,
            "attached": [b["hash"] for b in attach],
        }

    def resume_switch(self, deltas_by_hash: dict[str, list[dict]]) -> Optional[dict]:
        """Complete a switch whose DETACHED plan survived a crash/restart.

        Returns a description if a plan was resumed, else None.
        """
        with self._lock:
            plan = self.get_plan()
            if plan is None:
                return None
            if plan["phase"] != "DETACHED":
                # A PLANNED-but-uncommitted row cannot exist (rollback), but be
                # explicit: treat anything else as requiring operator review.
                raise RuntimeError(f"switch plan in unexpected phase {plan['phase']!r}")
            attach_hashes = json.loads(plan["attach_hashes_json"])
            attach_meta: list[dict] = []
            for h in attach_hashes:
                meta = self.get_block_meta(h)
                if meta is None:  # pragma: no cover - defensive
                    raise RuntimeError(f"attach block {h[:12]}… missing during resume")
                attach_meta.append(
                    {"hash": h, "height": int(meta["height"]), "weight": int(meta["weight"])}
                )
            self.finish_attach(attach=attach_meta, deltas_by_hash=deltas_by_hash)
            return {
                "new_tip": plan["new_tip"],
                "request_id": plan["request_id"],
                "attached": attach_hashes,
            }

    # ----------------------------------------------------- derived views
    def account_balance(self, address: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT balance FROM account_state WHERE address = ?", (address,)
            ).fetchone()
            return int(row["balance"]) if row else 0

    def account_nonce(self, address: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT nonce FROM account_state WHERE address = ?", (address,)
            ).fetchone()
            return int(row["nonce"]) if row else 0

    def accounts_snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                r["address"]: {"balance": int(r["balance"]), "nonce": int(r["nonce"])}
                for r in self.conn.execute(
                    "SELECT address, balance, nonce FROM account_state ORDER BY address"
                )
            }

    def events_for_address(self, address: str, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT seq, block_hash, height, txid, position_in_block, kind, "
                    "address, amount_delta, nonce_delta FROM derived_events "
                    "WHERE address = ? ORDER BY seq DESC LIMIT ?",
                    (address, limit),
                )
            )

    def tx_contributions(self, tx_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT txid, block_hash, height, on_active FROM tx_locations "
                    "WHERE txid = ? ORDER BY height",
                    (tx_id,),
                )
            )

    def active_tx_exists(self, tx_id: str) -> bool:
        with self._lock:
            return (
                self.conn.execute(
                    "SELECT 1 FROM tx_locations WHERE txid = ? AND on_active = 1 LIMIT 1",
                    (tx_id,),
                ).fetchone()
                is not None
            )

    def derived_event_count(self) -> int:
        with self._lock:
            return int(
                self.conn.execute("SELECT COUNT(*) c FROM derived_events").fetchone()["c"]
            )

    def all_derived_events(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT block_hash, height, txid, position_in_block, kind, address, "
                    "amount_delta, nonce_delta FROM derived_events ORDER BY seq"
                )
            )

    # ------------------------------------------------------ diagnostics
    def insert_diagnostic(self, record: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO diagnostics(request_id, outcome, reason, block_hash, "
                "height, parent, active_tip, active_height, weight, detail, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.get("request_id"),
                    record.get("outcome"),
                    record.get("reason"),
                    record.get("block_hash"),
                    record.get("height"),
                    record.get("parent"),
                    record.get("active_tip"),
                    record.get("active_height"),
                    record.get("weight"),
                    record.get("detail"),
                    record.get("created_at", utc_now()),
                ),
            )

    def diagnostics(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM diagnostics ORDER BY seq DESC LIMIT ?", (limit,)
                )
            )

    def diagnostics_for_request(self, request_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM diagnostics WHERE request_id = ? ORDER BY seq",
                    (request_id,),
                )
            )

    # --------------------------------------------------------- rebuild
    def wipe_derived_state(self) -> None:
        """Reset everything except the diagnostics log for rebuild tests."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for table in (
                    "derived_events",
                    "account_state",
                    "active_chain",
                    "tx_locations",
                    "pending_blocks",
                    "switch_plan",
                    "blocks",
                ):
                    self.conn.execute(f"DELETE FROM {table}")
                self.conn.execute(
                    "UPDATE counters SET value = 0 WHERE name = 'arrival'"
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
