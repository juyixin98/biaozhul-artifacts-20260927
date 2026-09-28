"""SQLite 索引存储（模块职责：持久化与索引一致性，不含池策略）。

关键设计
--------
* 单一连接 + WAL + 外键开启；所有状态迁移在一个 ``BEGIN IMMEDIATE`` 事务里完成，
  审计行与索引行同事务写入，保证"过期/淘汰不能破坏索引"——中途失败整批回滚。
* ``(sender, nonce)`` **部分唯一索引**只约束有效交易（pending/queued/proposed），
  因此同 nonce 旧交易进入 DROPPED 后会历史保留，但永远不会与新交易并存有效。
* ``schema_version`` 写在 meta 表，启动时与代码版本核对（当前仅 v1，不匹配即报错）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from eth_utils import to_checksum_address

from ..core.config import SCHEMA_VERSION
from ..core.models import (
    AccountState,
    AuditEvent,
    Block,
    DropReason,
    StoredTransaction,
    Transaction,
    TxStatus,
)

ACTIVE_STATUSES = (TxStatus.PENDING.value, TxStatus.QUEUED.value, TxStatus.PROPOSED.value)
POOL_STATUSES = (TxStatus.PENDING.value, TxStatus.QUEUED.value)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    address            TEXT PRIMARY KEY,
    balance            INTEGER NOT NULL,
    nonce              INTEGER NOT NULL,
    projected_balance  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_hash        TEXT PRIMARY KEY,
    sender         TEXT NOT NULL,
    nonce          INTEGER NOT NULL,
    gas_price      INTEGER NOT NULL,
    gas_limit      INTEGER NOT NULL,
    to_addr        TEXT NOT NULL,
    value          INTEGER NOT NULL,
    data           BLOB NOT NULL,
    v              INTEGER NOT NULL,
    r              BLOB NOT NULL,   -- uint256，超过 SQLite 64 位整数范围
    s              BLOB NOT NULL,   -- uint256
    raw            BLOB NOT NULL,
    status         TEXT NOT NULL,
    status_reason  TEXT NOT NULL DEFAULT '',
    received_at_ms INTEGER NOT NULL,  -- 首次进入系统的时间
    pending_since_ms INTEGER,         -- 最近一次进入 pending 的时间（TTL 用）
    updated_at_ms  INTEGER NOT NULL,
    proposed_block TEXT,
    drop_reason    TEXT,
    FOREIGN KEY (proposed_block) REFERENCES blocks(block_hash)
);

-- 有效交易（含已提议待确认）在 (sender, nonce) 上唯一：同 nonce 不可并存有效。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_sender_nonce
    ON transactions(sender, nonce)
    WHERE status IN ('pending', 'queued', 'proposed');

CREATE INDEX IF NOT EXISTS ix_tx_sender_status_nonce
    ON transactions(sender, status, nonce);
CREATE INDEX IF NOT EXISTS ix_tx_status_price
    ON transactions(status, gas_price, received_at_ms, tx_hash);

CREATE TABLE IF NOT EXISTS blocks (
    block_hash     TEXT PRIMARY KEY,
    number         INTEGER NOT NULL UNIQUE,
    parent_hash    TEXT NOT NULL,
    proposed_at_ms INTEGER NOT NULL,
    gas_used       INTEGER NOT NULL,
    confirmed      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS block_transactions (
    block_hash     TEXT NOT NULL,
    position       INTEGER NOT NULL,
    tx_hash        TEXT NOT NULL,
    applied        INTEGER NOT NULL,  -- 1=执行 0=执行期跳过
    PRIMARY KEY (block_hash, position),
    FOREIGN KEY (block_hash) REFERENCES blocks(block_hash),
    FOREIGN KEY (tx_hash) REFERENCES transactions(tx_hash)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    occurred_at_ms  INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    tx_hash         TEXT,
    block_hash      TEXT,
    reason          TEXT NOT NULL,
    module          TEXT NOT NULL,
    service_version TEXT NOT NULL,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_audit_tx ON audit_log(tx_hash, audit_id);
CREATE INDEX IF NOT EXISTS ix_audit_block ON audit_log(block_hash, audit_id);
CREATE INDEX IF NOT EXISTS ix_audit_request ON audit_log(request_id, audit_id);
"""


class IntegrityError(Exception):
    """存储不变量被破坏（正常代码路径不应触发；出现即说明有 bug）。"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if path.parent != Path("") and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row["value"]) != SCHEMA_VERSION:
        raise IntegrityError(
            f"数据库 schema 版本 {row['value']} 与代码 {SCHEMA_VERSION} 不兼容"
        )


# --------------------------------------------------------------------------- #
# 行映射
# --------------------------------------------------------------------------- #
def _row_to_tx(row: sqlite3.Row) -> StoredTransaction:
    tx = Transaction(
        tx_hash=row["tx_hash"],
        sender=to_checksum_address(row["sender"]),
        nonce=row["nonce"],
        gas_price=row["gas_price"],
        gas_limit=row["gas_limit"],
        to=row["to_addr"],
        value=row["value"],
        data=bytes(row["data"]),
        v=row["v"],
        r=int.from_bytes(bytes(row["r"]), "big"),
        s=int.from_bytes(bytes(row["s"]), "big"),
        raw=bytes(row["raw"]),
    )
    return StoredTransaction(
        tx=tx,
        status=TxStatus(row["status"]),
        received_at_ms=row["received_at_ms"],
        status_reason=row["status_reason"],
    )


def _row_to_account(row: sqlite3.Row) -> AccountState:
    return AccountState(
        address=to_checksum_address(row["address"]),
        balance=row["balance"],
        nonce=row["nonce"],
        projected_balance=row["projected_balance"],
    )


def _row_to_block(row: sqlite3.Row, tx_rows: Sequence[sqlite3.Row]) -> Block:
    applied = tuple(r["tx_hash"] for r in tx_rows if r["applied"])
    skipped = tuple(r["tx_hash"] for r in tx_rows if not r["applied"])
    return Block(
        block_hash=row["block_hash"],
        number=row["number"],
        parent_hash=row["parent_hash"],
        proposed_at_ms=row["proposed_at_ms"],
        executed_tx_hashes=applied,
        skipped_tx_hashes=skipped,
        gas_used=row["gas_used"],
    )


class Repository:
    """线程安全的存储门面。kernel 只通过本类读写。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    # ----- 事务边界 ----- #
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """串行化所有写事务（BEGIN IMMEDIATE），审计与状态同提交。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ----- 账户 ----- #
    def upsert_account(
        self,
        conn: sqlite3.Connection,
        address: str,
        balance: int,
        nonce: int = 0,
    ) -> AccountState:
        addr = to_checksum_address(address)
        conn.execute(
            """
            INSERT INTO accounts(address, balance, nonce, projected_balance)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                balance=excluded.balance,
                nonce=excluded.nonce,
                projected_balance=excluded.balance
            """,
            (addr, balance, nonce, balance),
        )
        return self.get_account(conn, addr)  # type: ignore[return-value]

    def credit_account(
        self, conn: sqlite3.Connection, address: str, amount: int
    ) -> AccountState:
        addr = to_checksum_address(address)
        conn.execute(
            "UPDATE accounts SET balance=balance+?, projected_balance=projected_balance+? "
            "WHERE address=?",
            (amount, amount, addr),
        )
        return self.get_account(conn, addr)  # type: ignore[return-value]

    def get_account(
        self, conn: sqlite3.Connection, address: str
    ) -> AccountState | None:
        row = conn.execute(
            "SELECT * FROM accounts WHERE address=?",
            (to_checksum_address(address),),
        ).fetchone()
        return _row_to_account(row) if row else None

    def require_account(
        self, conn: sqlite3.Connection, address: str
    ) -> AccountState:
        acct = self.get_account(conn, address)
        if acct is None:
            raise IntegrityError(f"账户缺失: {address}")
        return acct

    def list_accounts(self, conn: sqlite3.Connection) -> list[AccountState]:
        rows = conn.execute(
            "SELECT * FROM accounts ORDER BY address"
        ).fetchall()
        return [_row_to_account(r) for r in rows]

    # ----- 交易 ----- #
    def insert_transaction(
        self,
        conn: sqlite3.Connection,
        tx: Transaction,
        status: TxStatus,
        now_ms: int,
        reason: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO transactions(
                tx_hash, sender, nonce, gas_price, gas_limit, to_addr,
                value, data, v, r, s, raw, status, status_reason,
                received_at_ms, pending_since_ms, updated_at_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tx.tx_hash,
                tx.sender,
                tx.nonce,
                tx.gas_price,
                tx.gas_limit,
                tx.to,
                tx.value,
                tx.data,
                tx.v,
                tx.r.to_bytes(32, "big"),
                tx.s.to_bytes(32, "big"),
                tx.raw,
                status.value,
                reason,
                now_ms,
                now_ms if status is TxStatus.PENDING else None,
                now_ms,
            ),
        )

    def update_tx_status(
        self,
        conn: sqlite3.Connection,
        tx_hash: str,
        status: TxStatus,
        now_ms: int,
        reason: str,
        proposed_block: str | None = None,
        drop_reason: DropReason | None = None,
    ) -> None:
        # 进入 pending 时刷新 pending_since_ms；离开 pending 时清空，
        # 使 TTL 度量的是"连续停留 pending 的时长"而非出生绝对时间。
        if status is TxStatus.PENDING:
            conn.execute(
                """
                UPDATE transactions
                SET status=?, status_reason=?, updated_at_ms=?,
                    pending_since_ms=?,
                    proposed_block=COALESCE(?, proposed_block),
                    drop_reason=COALESCE(?, drop_reason)
                WHERE tx_hash=?
                """,
                (status.value, reason, now_ms, now_ms,
                 proposed_block, drop_reason.value if drop_reason else None,
                 tx_hash),
            )
        else:
            conn.execute(
                """
                UPDATE transactions
                SET status=?, status_reason=?, updated_at_ms=?,
                    pending_since_ms=NULL,
                    proposed_block=COALESCE(?, proposed_block),
                    drop_reason=COALESCE(?, drop_reason)
                WHERE tx_hash=?
                """,
                (status.value, reason, now_ms,
                 proposed_block, drop_reason.value if drop_reason else None,
                 tx_hash),
            )

    def clear_proposed_block(
        self, conn: sqlite3.Connection, tx_hashes: Sequence[str]
    ) -> None:
        if not tx_hashes:
            return
        conn.executemany(
            "UPDATE transactions SET proposed_block=NULL, updated_at_ms=updated_at_ms "
            "WHERE tx_hash=?",
            ((h,) for h in tx_hashes),
        )

    def get_tx(
        self, conn: sqlite3.Connection, tx_hash: str
    ) -> StoredTransaction | None:
        row = conn.execute(
            "SELECT * FROM transactions WHERE tx_hash=?", (tx_hash,)
        ).fetchone()
        return _row_to_tx(row) if row else None

    def get_active_by_sender_nonce(
        self, conn: sqlite3.Connection, sender: str, nonce: int
    ) -> StoredTransaction | None:
        row = conn.execute(
            """
            SELECT * FROM transactions
            WHERE sender=? AND nonce=? AND status IN (?,?,?)
            """,
            (
                to_checksum_address(sender),
                nonce,
                *ACTIVE_STATUSES,
            ),
        ).fetchone()
        return _row_to_tx(row) if row else None

    def get_by_hash_any_status(
        self, conn: sqlite3.Connection, tx_hash: str
    ) -> StoredTransaction | None:
        return self.get_tx(conn, tx_hash)

    def txs_for_sender(
        self,
        conn: sqlite3.Connection,
        sender: str,
        statuses: Sequence[TxStatus] | None = None,
    ) -> list[StoredTransaction]:
        statuses = statuses or list(TxStatus)
        marks = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"SELECT * FROM transactions WHERE sender=? AND status IN ({marks}) "
            f"ORDER BY nonce",
            (to_checksum_address(sender), *(s.value for s in statuses)),
        ).fetchall()
        return [_row_to_tx(r) for r in rows]

    def active_senders(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT DISTINCT sender FROM transactions "
            "WHERE status IN (?,?) ORDER BY sender",
            POOL_STATUSES,
        ).fetchall()
        return [to_checksum_address(r["sender"]) for r in rows]

    def all_pool_txs(
        self, conn: sqlite3.Connection
    ) -> list[StoredTransaction]:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE status IN (?,?) "
            "ORDER BY sender, nonce",
            POOL_STATUSES,
        ).fetchall()
        return [_row_to_tx(r) for r in rows]

    def pending_txs(
        self, conn: sqlite3.Connection
    ) -> list[StoredTransaction]:
        # 存储顺序按发送者/nonce（语义顺序）；费竞争排序由 ordering 引擎完成，
        # 避免展示与内部计算把同发送者交易按价格打散。
        rows = conn.execute(
            "SELECT * FROM transactions WHERE status='pending' "
            "ORDER BY sender, nonce"
        ).fetchall()
        return [_row_to_tx(r) for r in rows]

    def pool_count(self, conn: sqlite3.Connection) -> int:
        return int(
            conn.execute(
                "SELECT COUNT(*) c FROM transactions WHERE status IN (?,?)",
                POOL_STATUSES,
            ).fetchone()["c"]
        )

    def sender_pool_counts(
        self, conn: sqlite3.Connection, sender: str
    ) -> tuple[int, int]:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending_n,
              SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) queued_n
            FROM transactions
            WHERE sender=? AND status IN ('pending','queued')
            """,
            (to_checksum_address(sender),),
        ).fetchone()
        return int(row["pending_n"] or 0), int(row["queued_n"] or 0)

    def expired_pending(
        self, conn: sqlite3.Connection, pending_before_ms: int
    ) -> list[StoredTransaction]:
        """pending 且**进入 pending 的时刻**严格早于阈值的交易。"""
        rows = conn.execute(
            "SELECT * FROM transactions WHERE status='pending' "
            "AND pending_since_ms IS NOT NULL "
            "AND pending_since_ms < ? ORDER BY tx_hash",
            (pending_before_ms,),
        ).fetchall()
        return [_row_to_tx(r) for r in rows]

    def eviction_candidates(
        self, conn: sqlite3.Connection, *, include_pending: bool
    ) -> list[StoredTransaction]:
        """淘汰候选：永远先 queued（价低、早到）；可选再 pending。

        排序键刻意与候选区块排序方向相反，并以 tx_hash 兜底，保证跨运行确定性。
        """
        if include_pending:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE status IN ('pending','queued') "
                "ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, "
                "gas_price, received_at_ms, tx_hash"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE status='queued' "
                "ORDER BY gas_price, received_at_ms, tx_hash"
            ).fetchall()
        return [_row_to_tx(r) for r in rows]

    # ----- 区块 ----- #
    def insert_block(
        self,
        conn: sqlite3.Connection,
        block: Block,
        positions: Sequence[tuple[str, bool]],
        confirmed: bool,
    ) -> None:
        conn.execute(
            """
            INSERT INTO blocks(block_hash, number, parent_hash, proposed_at_ms,
                               gas_used, confirmed)
            VALUES(?,?,?,?,?,?)
            """,
            (
                block.block_hash,
                block.number,
                block.parent_hash,
                block.proposed_at_ms,
                block.gas_used,
                1 if confirmed else 0,
            ),
        )
        conn.executemany(
            "INSERT INTO block_transactions(block_hash, position, tx_hash, applied) "
            "VALUES(?,?,?,?)",
            [
                (block.block_hash, idx, tx_hash, 1 if applied else 0)
                for idx, (tx_hash, applied) in enumerate(positions)
            ],
        )

    def head_block(self, conn: sqlite3.Connection) -> Block | None:
        row = conn.execute(
            "SELECT * FROM blocks ORDER BY number DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        tx_rows = conn.execute(
            "SELECT * FROM block_transactions WHERE block_hash=? ORDER BY position",
            (row["block_hash"],),
        ).fetchall()
        return _row_to_block(row, tx_rows)

    def get_block_by_number(
        self, conn: sqlite3.Connection, number: int
    ) -> Block | None:
        row = conn.execute(
            "SELECT * FROM blocks WHERE number=?", (number,)
        ).fetchone()
        if row is None:
            return None
        tx_rows = conn.execute(
            "SELECT * FROM block_transactions WHERE block_hash=? ORDER BY position",
            (row["block_hash"],),
        ).fetchall()
        return _row_to_block(row, tx_rows)

    def get_block_by_hash(
        self, conn: sqlite3.Connection, block_hash: str
    ) -> Block | None:
        row = conn.execute(
            "SELECT * FROM blocks WHERE block_hash=?", (block_hash,)
        ).fetchone()
        if row is None:
            return None
        tx_rows = conn.execute(
            "SELECT * FROM block_transactions WHERE block_hash=? ORDER BY position",
            (block_hash,),
        ).fetchall()
        return _row_to_block(row, tx_rows)

    def blocks_above(
        self, conn: sqlite3.Connection, number_exclusive: int
    ) -> list[Block]:
        rows = conn.execute(
            "SELECT * FROM blocks WHERE number > ? ORDER BY number DESC",
            (number_exclusive,),
        ).fetchall()
        result: list[Block] = []
        for row in rows:
            tx_rows = conn.execute(
                "SELECT * FROM block_transactions WHERE block_hash=? ORDER BY position",
                (row["block_hash"],),
            ).fetchall()
            result.append(_row_to_block(row, tx_rows))
        return result

    def mark_confirmed(
        self, conn: sqlite3.Connection, block_hashes: Sequence[str]
    ) -> None:
        conn.executemany(
            "UPDATE blocks SET confirmed=1 WHERE block_hash=?",
            ((h,) for h in block_hashes),
        )

    def delete_blocks(
        self, conn: sqlite3.Connection, block_hashes: Sequence[str]
    ) -> None:
        if not block_hashes:
            return
        marks = ",".join("?" for _ in block_hashes)
        conn.execute(
            f"DELETE FROM block_transactions WHERE block_hash IN ({marks})",
            block_hashes,
        )
        conn.execute(
            f"DELETE FROM blocks WHERE block_hash IN ({marks})", block_hashes
        )

    # ----- 审计 ----- #
    def append_audit(
        self,
        conn: sqlite3.Connection,
        *,
        request_id: str,
        now_ms: int,
        event_type: str,
        reason: str,
        tx_hash: str | None = None,
        block_hash: str | None = None,
        detail: dict[str, Any] | None = None,
        module: str = "local_txpool",
        service_version: str = "1.0.0",
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO audit_log(request_id, occurred_at_ms, event_type,
                tx_hash, block_hash, reason, module, service_version, detail_json)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                request_id,
                now_ms,
                event_type,
                tx_hash,
                block_hash,
                reason,
                module,
                service_version,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return int(cur.lastrowid or 0)

    def audit_since(
        self,
        conn: sqlite3.Connection,
        after_audit_id: int = 0,
        limit: int = 10_000,
    ) -> list[AuditEvent]:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE audit_id > ? ORDER BY audit_id LIMIT ?",
            (after_audit_id, limit),
        ).fetchall()
        return [
            AuditEvent(
                audit_id=r["audit_id"],
                request_id=r["request_id"],
                occurred_at_ms=r["occurred_at_ms"],
                event_type=r["event_type"],
                tx_hash=r["tx_hash"],
                block_hash=r["block_hash"],
                reason=r["reason"],
                detail=json.loads(r["detail_json"] or "{}"),
                module=r["module"],
                service_version=r["service_version"],
            )
            for r in rows
        ]

    def audit_for_request(
        self, conn: sqlite3.Connection, request_id: str
    ) -> list[AuditEvent]:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE request_id=? ORDER BY audit_id",
            (request_id,),
        ).fetchall()
        return [
            AuditEvent(
                audit_id=r["audit_id"],
                request_id=r["request_id"],
                occurred_at_ms=r["occurred_at_ms"],
                event_type=r["event_type"],
                tx_hash=r["tx_hash"],
                block_hash=r["block_hash"],
                reason=r["reason"],
                detail=json.loads(r["detail_json"] or "{}"),
                module=r["module"],
                service_version=r["service_version"],
            )
            for r in rows
        ]

    # ----- 不变量体检 ----- #
    def assert_integrity(self, conn: sqlite3.Connection) -> None:
        """索引/余额不变量自检；任何违反都抛 :class:`IntegrityError`。

        仅用于测试与回放复核（全表扫描），不在热路径调用。
        """
        problems: list[str] = []

        # 1) 每个 (sender, nonce) 至多一条有效交易（由部分唯一索引兜底，
        #    这里再用聚合查一遍给出可读错误）。
        rows = conn.execute(
            """
            SELECT sender, nonce, COUNT(*) c FROM transactions
            WHERE status IN ('pending','queued','proposed')
            GROUP BY sender, nonce HAVING c > 1
            """
        ).fetchall()
        for r in rows:
            problems.append(
                f"同 nonce 有效交易并存: {r['sender']} nonce={r['nonce']} x{r['c']}"
            )

        # 2) 有效 nonce 不重复；pending 从账户 nonce 起连续；
        #    queued 不得与 proposed/pending 占同一 nonce。
        #    （queued 允许有任意远的缺口——它们本就"在缺口之后等待"。）
        for acct in self.list_accounts(conn):
            active = self.txs_for_sender(
                conn,
                acct.address,
                [TxStatus.PENDING, TxStatus.QUEUED, TxStatus.PROPOSED],
            )
            # 同 (sender,nonce) 唯一由部分索引保证；这里再查可读性。
            seen: dict[int, str] = {}
            for stx in active:
                if stx.tx.nonce in seen:
                    problems.append(
                        f"有效 nonce 重复: {acct.address} "
                        f"nonce={stx.tx.nonce} "
                        f"({seen[stx.tx.nonce]} vs {stx.status.value})"
                    )
                seen[stx.tx.nonce] = stx.status.value

            pending_nonces = sorted(
                stx.tx.nonce
                for stx in active
                if stx.status is TxStatus.PENDING
            )
            for offset, n in enumerate(pending_nonces):
                if n != acct.nonce + offset:
                    problems.append(
                        f"pending 前缀不连续: {acct.address} "
                        f"nonce={n} 期望 {acct.nonce + offset}"
                    )
                    break
            occupied = set(pending_nonces) | {
                stx.tx.nonce
                for stx in active
                if stx.status is TxStatus.PROPOSED
            }
            for stx in active:
                if stx.status is TxStatus.QUEUED and stx.tx.nonce in occupied:
                    problems.append(
                        f"queued 与 pending/proposed 占用同 nonce: "
                        f"{acct.address} nonce={stx.tx.nonce}"
                    )
                if stx.status is TxStatus.PROPOSED and stx.tx.nonce >= acct.nonce:
                    problems.append(
                        f"proposed nonce 未小于账户 nonce: "
                        f"{acct.address} nonce={stx.tx.nonce} "
                        f"account_nonce={acct.nonce}"
                    )

        # 3) projected_balance <= balance，且差额恰等于 pending 承诺额。
        for acct in self.list_accounts(conn):
            if acct.projected_balance > acct.balance:
                problems.append(
                    f"projected_balance 超过 balance: {acct.address}"
                )
            if acct.projected_balance < 0:
                problems.append(f"projected_balance 为负: {acct.address}")
            row = conn.execute(
                """
                SELECT COALESCE(SUM(gas_limit*gas_price + value),0) committed
                FROM transactions
                WHERE sender=? AND status='pending'
                """,
                (acct.address,),
            ).fetchone()
            committed = int(row["committed"])
            if acct.balance - acct.projected_balance != committed:
                problems.append(
                    f"承诺金额不一致: {acct.address} 账面承诺="
                    f"{acct.balance - acct.projected_balance} 重算={committed}"
                )

        # 4) proposed 交易必须能在 block_transactions 中找到。
        rows = conn.execute(
            """
            SELECT t.tx_hash FROM transactions t
            LEFT JOIN block_transactions b
              ON b.tx_hash=t.tx_hash
            WHERE t.status='proposed' AND b.tx_hash IS NULL
            """
        ).fetchall()
        for r in rows:
            problems.append(f"proposed 交易缺少区块关联: {r['tx_hash']}")

        if problems:
            raise IntegrityError("; ".join(problems))
