"""索引存储边界 (index storage boundary)。

职责（只做持久化与查询，不做共识规则判定）：
* ``utxo``   —— 存活 UTXO（主键 outpoint），含 pubkey 索引与花费审计标记；
* ``spent``  —— 已花费审计表，用于区分"未知 outpoint"与"已花费 outpoint"；
* ``blocks`` —— 已提交完整块（height/block_id 唯一）；
* ``tx_index``—— txid -> (height, position)，防重复交易；
* ``meta``   —— tip 等单值状态。

原子性：一次 :meth:`SqliteStore.apply_block` 对应单个 sqlite 事务
（BEGIN IMMEDIATE ... COMMIT）。任何异常都 ROLLBACK —— 内核在调用前已完成
全部校验，因此这里的约束冲突只作为兜底，发生即报 COMPUTATION_FAILED。

存储层不 import 内核模块（避免环依赖）；它消费计划对象暴露的只读属性
（``committed_inputs`` / ``new_outputs`` / ``block``），靠结构契约工作。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Protocol

from . import encoding
from .errors import (
    BlockConflictError,
    StorageFailureError,
    UnknownOutpointError,
)

if TYPE_CHECKING:  # 仅类型；运行时不依赖内核，杜绝循环 import
    from .encoding import Block


@dataclass(frozen=True, slots=True)
class Utxo:
    """存活 UTXO 记录（内核与 API 共用的存储读取契约）。"""

    txid: bytes
    vout: int
    amount: int
    pubkey: bytes
    created_height: int


class ChainView(Protocol):
    """内核规划时依赖的只读链视图（SqliteStore 满足该协议）。"""

    def tip(self) -> tuple[int, bytes] | None: ...

    def classify_outpoint(self, txid: bytes, vout: int) -> str:
        """返回 'unspent' | 'spent' | 'unknown'。"""
        ...

    def get_utxo(self, txid: bytes, vout: int) -> Utxo | None: ...

    def utxo_by_pubkey(self, pubkey: bytes) -> list[Utxo]: ...

    def live_utxo_rows(self) -> list[bytes]: ...

    def utxo_root(self) -> bytes: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS utxo (
    txid           BLOB NOT NULL,
    vout           INTEGER NOT NULL,
    amount         INTEGER NOT NULL,
    pubkey         BLOB NOT NULL,
    created_height INTEGER NOT NULL,
    PRIMARY KEY (txid, vout)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_utxo_pubkey ON utxo(pubkey);
CREATE TABLE IF NOT EXISTS spent (
    txid           BLOB NOT NULL,
    vout           INTEGER NOT NULL,
    spending_txid  BLOB NOT NULL,
    block_height   INTEGER NOT NULL,
    PRIMARY KEY (txid, vout)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS blocks (
    height          INTEGER PRIMARY KEY,
    block_id        BLOB NOT NULL UNIQUE,
    prev_hash       BLOB NOT NULL,
    tx_count        INTEGER NOT NULL,
    utxo_root_after BLOB NOT NULL,
    data            BLOB NOT NULL,
    committed_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tx_index (
    txid         BLOB PRIMARY KEY,
    block_height INTEGER NOT NULL,
    position     INTEGER NOT NULL,
    fee          INTEGER NOT NULL
) WITHOUT ROWID;
"""


class SqliteStore:
    """单连接 SQLite 存储。线程模型：调用方串行使用（API 层由应用持锁）。"""

    def __init__(self, path: str = ":memory:") -> None:
        try:
            self._conn = sqlite3.connect(
                path, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise StorageFailureError(f"无法打开数据库: {path}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise StorageFailureError("数据库初始化失败") from exc

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            raise StorageFailureError("关闭数据库失败") from exc

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------
    def tip(self) -> tuple[int, bytes] | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='tip_height'"
        ).fetchone()
        if row is None:
            return None
        height = int(row["value"])
        brow = self._conn.execute(
            "SELECT block_id FROM blocks WHERE height=?", (height,)
        ).fetchone()
        if brow is None:  # pragma: no cover - 不变量
            raise StorageFailureError("tip 元数据与块表不一致")
        return height, brow["block_id"]

    def classify_outpoint(self, txid: bytes, vout: int) -> str:
        row = self._conn.execute(
            "SELECT 1 FROM utxo WHERE txid=? AND vout=?", (txid, vout)
        ).fetchone()
        if row is not None:
            return "unspent"
        row = self._conn.execute(
            "SELECT 1 FROM spent WHERE txid=? AND vout=?", (txid, vout)
        ).fetchone()
        if row is not None:
            return "spent"
        return "unknown"

    def get_utxo(self, txid: bytes, vout: int) -> Utxo | None:
        row = self._conn.execute(
            "SELECT amount, pubkey, created_height FROM utxo WHERE txid=? AND vout=?",
            (txid, vout),
        ).fetchone()
        if row is None:
            return None
        return Utxo(
            txid=txid,
            vout=vout,
            amount=row["amount"],
            pubkey=row["pubkey"],
            created_height=row["created_height"],
        )

    def require_utxo(self, txid: bytes, vout: int) -> Utxo:
        utxo = self.get_utxo(txid, vout)
        if utxo is None:
            raise UnknownOutpointError(
                "outpoint 不存在或已花费",
                details={"txid": txid.hex(), "vout": vout},
            )
        return utxo

    def utxo_by_pubkey(self, pubkey: bytes) -> list[Utxo]:
        rows = self._conn.execute(
            "SELECT txid, vout, amount, created_height FROM utxo "
            "WHERE pubkey=? ORDER BY txid, vout",
            (pubkey,),
        ).fetchall()
        return [
            Utxo(
                txid=r["txid"],
                vout=r["vout"],
                amount=r["amount"],
                pubkey=pubkey,
                created_height=r["created_height"],
            )
            for r in rows
        ]

    def utxo_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) c FROM utxo").fetchone()["c"])

    def block_count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) c FROM blocks").fetchone()["c"]
        )

    def get_block(self, height: int) -> bytes | None:
        row = self._conn.execute(
            "SELECT data FROM blocks WHERE height=?", (height,)
        ).fetchone()
        return None if row is None else row["data"]

    def has_tx(self, txid: bytes) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tx_index WHERE txid=?", (txid,)
        ).fetchone()
        return row is not None

    def live_utxo_rows(self) -> list[bytes]:
        rows = self._conn.execute(
            "SELECT txid, vout, amount, pubkey FROM utxo "
            "ORDER BY txid ASC, vout ASC"
        ).fetchall()
        return [
            encoding.utxo_row(r["txid"], r["vout"], r["amount"], r["pubkey"])
            for r in rows
        ]

    def utxo_root(self) -> bytes:
        return encoding.utxo_root(self.live_utxo_rows())

    # ------------------------------------------------------------------
    # 原子写入
    # ------------------------------------------------------------------
    def apply_block(self, plan: Any) -> bytes:
        """按内核计划原子应用整块。

        plan 结构契约（鸭子类型，见 :class:`utxo_ledger.kernel.BlockPlan`）：
            plan.block                         Block
            plan.block_id                      bytes
            plan.results[i].txid               bytes
            plan.results[i].tx                 Transaction
            plan.results[i].committed_inputs   list[Utxo]   被本 tx 花费的存活 UTXO
            plan.results[i].new_outputs        list[(vout, TxOutput)]
        返回提交后的 UTXO 根。任何失败回滚并抛出，调用方保证状态与提交前一致。
        """
        block: Block = plan.block
        height = block.header.height
        block_id: bytes = plan.block_id
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")

            # 兜底冲突检查（内核已查过，这里只防并发/实现缺陷）
            if conn.execute(
                "SELECT 1 FROM blocks WHERE height=? OR block_id=?",
                (height, block_id),
            ).fetchone():
                raise BlockConflictError(
                    "块高度或 block_id 已存在",
                    details={"height": height, "block_id": block_id.hex()},
                )

            for pos, res in enumerate(plan.results):
                txid = res.txid
                if conn.execute(
                    "SELECT 1 FROM tx_index WHERE txid=?", (txid,)
                ).fetchone():
                    raise BlockConflictError(
                        "交易已存在于历史块", details={"txid": txid.hex()}
                    )
                for u in res.committed_inputs:
                    cur = conn.execute(
                        "SELECT amount FROM utxo WHERE txid=? AND vout=?",
                        (u.txid, u.vout),
                    ).fetchone()
                    if cur is None:
                        raise BlockConflictError(
                            "提交时发现 outpoint 已不存活（兜底）",
                            details={"txid": u.txid.hex(), "vout": u.vout},
                        )
                    conn.execute(
                        "DELETE FROM utxo WHERE txid=? AND vout=?",
                        (u.txid, u.vout),
                    )
                    conn.execute(
                        "INSERT INTO spent(txid, vout, spending_txid, block_height) "
                        "VALUES(?,?,?,?)",
                        (u.txid, u.vout, txid, height),
                    )
                for vout, out in res.new_outputs:
                    conn.execute(
                        "INSERT INTO utxo(txid, vout, amount, pubkey, created_height) "
                        "VALUES(?,?,?,?,?)",
                        (txid, vout, out.amount, out.pubkey, height),
                    )
                conn.execute(
                    "INSERT INTO tx_index(txid, block_height, position, fee) "
                    "VALUES(?,?,?,?)",
                    (txid, height, pos, res.tx.fee),
                )

            root_after = encoding.utxo_root(
                [
                    encoding.utxo_row(r["txid"], r["vout"], r["amount"], r["pubkey"])
                    for r in conn.execute(
                        "SELECT txid, vout, amount, pubkey FROM utxo "
                        "ORDER BY txid ASC, vout ASC"
                    ).fetchall()
                ]
            )
            conn.execute(
                "INSERT INTO blocks(height, block_id, prev_hash, tx_count, "
                "utxo_root_after, data, committed_at) VALUES(?,?,?,?,?,?,?)",
                (
                    height,
                    block_id,
                    block.header.prev_hash,
                    len(plan.results),
                    root_after,
                    encoding.encode_block(block),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('tip_height', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(height),),
            )
            conn.execute("COMMIT")
            return root_after
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    def iter_blocks(self) -> Iterable[tuple[int, bytes]]:
        for row in self._conn.execute(
            "SELECT height, data FROM blocks ORDER BY height"
        ):
            yield row["height"], row["data"]
