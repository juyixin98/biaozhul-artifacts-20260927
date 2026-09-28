"""SQLite 仓储：所有持久化与索引查询集中于此，内核不直接写 SQL。

关键完整性由数据库保证：
* ``ux_active_sender_nonce`` 部分唯一索引：同一发送者同一 nonce 在
  pending/queued/included 中至多一条——同 nonce 不同交易不可能并存有效。
* 金额以十进制 TEXT 存 wei；排序/比较处显式 ``CAST(... AS INTEGER)``。
* 所有多表变更由调用方包在 :meth:`Repository.transaction` 中（BEGIN IMMEDIATE），
  确认/回滚等多步操作原子提交。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

from ..core import TxRecord
from ..core import BLOCK_CONFIRMED, BLOCK_PROPOSED, POOL_STATUSES


class UniqueActiveViolation(Exception):
    """(sender, nonce) 活跃唯一索引冲突。"""


def _load_schema() -> str:
    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


class Repository:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(_load_schema())

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # 事务
    # ------------------------------------------------------------------ #
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ #
    # metadata
    # ------------------------------------------------------------------ #
    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------------ #
    # 账户
    # ------------------------------------------------------------------ #
    def get_account(self, address: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT address, balance, nonce FROM accounts WHERE address=?",
            (address.lower(),),
        ).fetchone()

    def all_accounts(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT address, balance, nonce FROM accounts ORDER BY address"))

    def ensure_account(self, address: str, ts: int, *, balance: int = 0, nonce: int = 0) -> None:
        self._conn.execute(
            "INSERT INTO accounts(address, balance, nonce, created_at, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(address) DO NOTHING",
            (address.lower(), str(balance), nonce, ts, ts),
        )

    def adjust_balance(self, address: str, delta: int, ts: int) -> int:
        """对（可能不存在的）账户增减余额，返回新余额。

        不在 SQL 里做算术：SQLite 的 CAST 不识别大整数科学计数法，而余额以
        十进制 TEXT 存储，故在 Python 侧完成整数运算后整体写回。
        """
        address = address.lower()
        self.ensure_account(address, ts)
        row = self.get_account(address)
        new_balance = int(row["balance"]) + int(delta)
        self._conn.execute(
            "UPDATE accounts SET balance=?, updated_at=? WHERE address=?",
            (str(new_balance), ts, address),
        )
        return new_balance

    def set_account(self, address: str, balance: int, nonce: int, ts: int) -> None:
        address = address.lower()
        self.ensure_account(address, ts)
        self._conn.execute(
            "UPDATE accounts SET balance=?, nonce=?, updated_at=? WHERE address=?",
            (str(balance), nonce, ts, address),
        )

    def snapshot_account(self, block_number: int, address: str) -> None:
        address = address.lower()
        row = self.get_account(address)
        if row is None:  # 快照时账户尚不存在：记为零值，回滚后删除由调用方决定
            balance, nonce = "0", 0
        else:
            balance, nonce = row["balance"], row["nonce"]
        self._conn.execute(
            "INSERT OR IGNORE INTO account_snapshots(block_number, address, balance_before, nonce_before) "
            "VALUES(?,?,?,?)",
            (block_number, address, balance, nonce),
        )

    def snapshots(self, block_number: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT address, balance_before, nonce_before FROM account_snapshots "
                "WHERE block_number=? ORDER BY address",
                (block_number,),
            )
        )

    def delete_snapshots(self, block_number: int) -> None:
        self._conn.execute("DELETE FROM account_snapshots WHERE block_number=?", (block_number,))

    # ------------------------------------------------------------------ #
    # 交易写入 / 读取
    # ------------------------------------------------------------------ #
    def insert_tx(self, fields: dict) -> None:
        try:
            self._conn.execute(
                """
                INSERT INTO transactions(
                    tx_hash, raw, sender, to_addr, nonce, gas_price, gas_limit, value,
                    data, chain_id, received_at, expires_at, status, reason,
                    reason_detail, replaced_by, block_number, position, updated_at)
                VALUES(:tx_hash,:raw,:sender,:to_addr,:nonce,:gas_price,:gas_limit,:value,
                    :data,:chain_id,:received_at,:expires_at,:status,:reason,
                    :reason_detail,:replaced_by,:block_number,:position,:updated_at)
                """,
                fields,
            )
        except sqlite3.IntegrityError as exc:
            raise UniqueActiveViolation(str(exc)) from exc

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TxRecord:
        return TxRecord(
            tx_hash=row["tx_hash"],
            raw=bytes(row["raw"]),
            sender=row["sender"],
            to_addr=row["to_addr"],
            nonce=row["nonce"],
            gas_price=int(row["gas_price"]),
            gas_limit=row["gas_limit"],
            value=int(row["value"]),
            data=bytes(row["data"]),
            chain_id=row["chain_id"],
            received_at=row["received_at"],
            expires_at=row["expires_at"],
            status=row["status"],
            reason=row["reason"],
            reason_detail=row["reason_detail"],
            replaced_by=row["replaced_by"],
            block_number=row["block_number"],
            position=row["position"],
        )

    def get_tx(self, tx_hash: str) -> TxRecord | None:
        row = self._conn.execute(
            "SELECT * FROM transactions WHERE tx_hash=?", (tx_hash.lower(),)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_active(self, sender: str, nonce: int) -> TxRecord | None:
        row = self._conn.execute(
            "SELECT * FROM transactions WHERE sender=? AND nonce=? "
            "AND status IN ('pending','queued','included')",
            (sender.lower(), nonce),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def list_sender(self, sender: str, statuses: tuple[str, ...]) -> list[TxRecord]:
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM transactions WHERE sender=? AND status IN ({marks}) ORDER BY nonce",
            (sender.lower(), *statuses),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_all(self, statuses: tuple[str, ...]) -> list[TxRecord]:
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM transactions WHERE status IN ({marks})", statuses
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_pool(self) -> list[TxRecord]:
        return self.list_all(POOL_STATUSES)

    def list_expirable(self, now: int) -> list[TxRecord]:
        rows = self._conn.execute(
            "SELECT * FROM transactions WHERE status IN ('pending','queued','included') "
            "AND expires_at <= ? ORDER BY expires_at, tx_hash",
            (now,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count_status(self, statuses: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in statuses)
        return self._conn.execute(
            f"SELECT COUNT(*) c FROM transactions WHERE status IN ({marks})", statuses
        ).fetchone()["c"]

    def count_sender_status(self, sender: str, statuses: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in statuses)
        return self._conn.execute(
            f"SELECT COUNT(*) c FROM transactions WHERE sender=? AND status IN ({marks})",
            (sender.lower(), *statuses),
        ).fetchone()["c"]

    def cheapest_queued(self, *, exclude_sender: str | None = None) -> TxRecord | None:
        """全局最便宜 queued 交易（容量淘汰候选）。

        排序：gas_price 升；同价最早到期；再同价按 hash 字典序——完全确定。
        """
        sql = (
            "SELECT * FROM transactions WHERE status='queued' "
            + ("AND sender != ? " if exclude_sender else "")
            + "ORDER BY CAST(gas_price AS INTEGER), expires_at, tx_hash LIMIT 1"
        )
        params = (exclude_sender.lower(),) if exclude_sender else ()
        row = self._conn.execute(sql, params).fetchone()
        return self._row_to_record(row) if row else None

    def cheapest_queued_for_sender(self, sender: str) -> TxRecord | None:
        row = self._conn.execute(
            "SELECT * FROM transactions WHERE status='queued' AND sender=? "
            "ORDER BY CAST(gas_price AS INTEGER), nonce LIMIT 1",
            (sender.lower(),),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def pending_senders(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT sender FROM transactions WHERE status='pending'"
        ).fetchall()
        return [r["sender"] for r in rows]

    def set_tx_status(
        self,
        tx_hash: str,
        status: str,
        reason: str,
        detail: str,
        ts: int,
        *,
        replaced_by: str | None = None,
        block_number: int | None = None,
        position: int | None = None,
        clear_assignment: bool = False,
    ) -> None:
        assignment = "block_number=?, position=?"
        params = [block_number, position]
        if clear_assignment:
            assignment = "block_number=NULL, position=NULL"
            params = []
        self._conn.execute(
            f"""
            UPDATE transactions
               SET status=?, reason=?, reason_detail=?,
                   {assignment}, updated_at=?
             WHERE tx_hash=?
            """,
            [status, reason, detail, *params, ts, tx_hash.lower()],
        )
        if replaced_by is not None:
            self._conn.execute(
                "UPDATE transactions SET replaced_by=? WHERE tx_hash=?",
                (replaced_by, tx_hash.lower()),
            )

    def clear_block_assignment(self, tx_hash: str) -> None:
        self._conn.execute(
            "UPDATE transactions SET block_number=NULL, position=NULL WHERE tx_hash=?",
            (tx_hash.lower(),),
        )

    # ------------------------------------------------------------------ #
    # 区块
    # ------------------------------------------------------------------ #
    def insert_block(self, number: int, block_hash: str, parent_hash: str,
                     gas_limit: int, gas_used: int, coinbase: str, ts: int) -> None:
        self._conn.execute(
            "INSERT INTO blocks(number, hash, parent_hash, gas_limit, gas_used, coinbase, "
            "status, proposed_at, confirmed_at) VALUES(?,?,?,?,?,?,?,?,NULL)",
            (number, block_hash, parent_hash, gas_limit, gas_used, coinbase.lower(),
             BLOCK_PROPOSED, ts),
        )

    def get_block(self, number: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM blocks WHERE number=?", (number,)).fetchone()

    def head_block(self) -> sqlite3.Row | None:
        """已确认链的链尖（proposed 区块不计入链高）。"""
        return self._conn.execute(
            "SELECT * FROM blocks WHERE status='confirmed' ORDER BY number DESC LIMIT 1"
        ).fetchone()

    def tip_block(self) -> sqlite3.Row | None:
        """最大编号区块（含 proposed），用于编号分配。"""
        return self._conn.execute("SELECT * FROM blocks ORDER BY number DESC LIMIT 1").fetchone()

    def latest_by_status(self, status: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM blocks WHERE status=? ORDER BY number DESC LIMIT 1", (status,)
        ).fetchone()

    def confirm_block(self, number: int, ts: int) -> None:
        self._conn.execute(
            "UPDATE blocks SET status=?, confirmed_at=? WHERE number=?",
            (BLOCK_CONFIRMED, ts, number),
        )

    def insert_block_tx(self, number: int, position: int, tx_hash: str) -> None:
        self._conn.execute(
            "INSERT INTO block_txs(block_number, position, tx_hash) VALUES(?,?,?)",
            (number, position, tx_hash.lower()),
        )

    def list_block_txs(self, number: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT tx_hash FROM block_txs WHERE block_number=? ORDER BY position", (number,)
        ).fetchall()
        return [r["tx_hash"] for r in rows]

    def delete_block(self, number: int) -> None:
        # ON DELETE CASCADE 连带删除 block_txs
        self._conn.execute("DELETE FROM blocks WHERE number=?", (number,))

    # ------------------------------------------------------------------ #
    # 流水日志（每次移入移出的理由）
    # ------------------------------------------------------------------ #
    def add_journal(self, *, ts: int, request_id: str, action: str,
                    tx_hash: str | None = None, sender: str | None = None,
                    block_number: int | None = None,
                    from_status: str = "", to_status: str = "",
                    reason: str = "", detail: dict | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO journals(ts, request_id, tx_hash, sender, block_number, action,
                                 from_status, to_status, reason, detail)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (ts, request_id, tx_hash, sender, block_number, action, from_status,
             to_status, reason, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)),
        )

    def query_journals(self, *, tx_hash: str | None = None, request_id: str | None = None,
                       block_number: int | None = None, sender: str | None = None,
                       limit: int = 200) -> list[sqlite3.Row]:
        clauses, params = [], []
        if tx_hash:
            clauses.append("tx_hash=?"); params.append(tx_hash.lower())
        if request_id:
            clauses.append("request_id=?"); params.append(request_id)
        if block_number is not None:
            clauses.append("block_number=?"); params.append(block_number)
        if sender:
            clauses.append("sender=?"); params.append(sender.lower())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return list(
            self._conn.execute(
                f"SELECT * FROM journals {where} ORDER BY id DESC LIMIT ?", params
            )
        )
