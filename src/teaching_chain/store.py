"""SQLite 索引存储。

职责（与内核严格分离）：

* 持久化**区块头、交易、收据**与链尖指针，提供按高度 / 哈希的索引查询；
* 只做追加写：回滚（分叉处理）在教学范围内不支持，重复提交同一高度将报错；
* 用 WAL + 单写连接保证本地一致性；
* 不实现执行逻辑——离线回放（``replay.py``）从这里读出原始交易后
  交由内核重新执行并逐笔比对。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .kernel import GENESIS_PARENT, Block, Receipt, transaction_to_external
from . import encoding

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    number       INTEGER PRIMARY KEY,
    hash         TEXT NOT NULL UNIQUE,
    parent_hash  TEXT NOT NULL,
    header_json  TEXT NOT NULL,
    tx_count     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    block_number INTEGER NOT NULL,
    tx_index     INTEGER NOT NULL,
    tx_hash      TEXT NOT NULL UNIQUE,
    tx_json      TEXT NOT NULL,
    PRIMARY KEY (block_number, tx_index)
);
CREATE TABLE IF NOT EXISTS receipts (
    block_number INTEGER NOT NULL,
    tx_index     INTEGER NOT NULL,
    tx_hash      TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    PRIMARY KEY (block_number, tx_index)
);
"""

META_CHAIN = "chain"
META_HEAD = "head_number"
META_HEAD_HASH = "head_hash"


class IndexError_(RuntimeError):
    """索引存储一致性 / 追加冲突错误。"""


def _receipt_from_dict(data: dict[str, Any]) -> Receipt:
    return Receipt(
        receipt_version=data["receipt_version"],
        program_version=data["program_version"],
        chain=data["chain"],
        block_number=data["block_number"],
        tx_index=data["tx_index"],
        tx_hash=data["tx_hash"],
        input_digest=data["input_digest"],
        caller=data["caller"],
        intrinsic_gas=data["intrinsic_gas"],
        gas_limit=data["gas_limit"],
        status=data["status"],
        gas_used=data["gas_used"],
        error_category=data.get("error_category"),
        error_pc=data.get("error_pc", -1),
        return_value=data.get("return_value", 0),
        state_root=data["state_root"],
        trace=tuple(data.get("trace", [])),
    )


class IndexStore:
    """线程安全（进程内锁）的 SQLite 索引。仅供单节点本地使用。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # 初始化与链尖
    # ------------------------------------------------------------------
    def initialize(self, chain: str) -> None:
        """空库时写入链号与创世指针；重复打开时校验链号一致。"""
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (META_CHAIN,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)", (META_CHAIN, chain)
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)", (META_HEAD, "-1")
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    (META_HEAD_HASH, GENESIS_PARENT),
                )
            elif row["value"] != chain:
                raise IndexError_(
                    f"库中链号为 {row['value']!r}，与 {chain!r} 冲突：拒绝复用"
                )

    @property
    def chain(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (META_CHAIN,)
            ).fetchone()
        if row is None:
            raise IndexError_("索引尚未初始化")
        return row["value"]

    def head(self) -> tuple[int, str]:
        with self._lock:
            n = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (META_HEAD,)
            ).fetchone()["value"]
            h = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (META_HEAD_HASH,)
            ).fetchone()["value"]
        return int(n), h

    # ------------------------------------------------------------------
    # 追加区块
    # ------------------------------------------------------------------
    def append_block(self, block: Block) -> None:
        header = block.header_dict()
        with self._tx() as conn:
            head_num, head_hash = self._head_locked(conn)
            if block.number != head_num + 1:
                raise IndexError_(
                    f"区块号 {block.number} 不是链尖 {head_num} 的下一块"
                )
            if block.parent_hash != head_hash:
                raise IndexError_("父哈希与当前链尖不一致")
            exists = conn.execute(
                "SELECT 1 FROM blocks WHERE number=?", (block.number,)
            ).fetchone()
            if exists is not None:
                raise IndexError_(f"区块 {block.number} 已存在（只支持追加）")

            conn.execute(
                "INSERT INTO blocks(number, hash, parent_hash, header_json, tx_count)"
                " VALUES (?,?,?,?,?)",
                (
                    block.number,
                    block.hash(),
                    block.parent_hash,
                    json.dumps(header, ensure_ascii=False, sort_keys=True),
                    len(block.transactions),
                ),
            )
            for idx, (tx, receipt) in enumerate(zip(block.transactions, block.receipts)):
                receipt_dict = receipt.to_dict()
                conn.execute(
                    "INSERT INTO transactions(block_number, tx_index, tx_hash, tx_json)"
                    " VALUES (?,?,?,?)",
                    (
                        block.number,
                        idx,
                        receipt.tx_hash,
                        json.dumps(transaction_to_external(tx),
                                   ensure_ascii=False, sort_keys=True),
                    ),
                )
                conn.execute(
                    "INSERT INTO receipts(block_number, tx_index, tx_hash, receipt_json, receipt_hash)"
                    " VALUES (?,?,?,?,?)",
                    (
                        block.number,
                        idx,
                        receipt.tx_hash,
                        json.dumps(receipt_dict, ensure_ascii=False, sort_keys=True),
                        receipt.digest(),
                    ),
                )
            conn.execute(
                "UPDATE meta SET value=? WHERE key=?", (str(block.number), META_HEAD)
            )
            conn.execute(
                "UPDATE meta SET value=? WHERE key=?", (block.hash(), META_HEAD_HASH)
            )

    @staticmethod
    def _head_locked(conn: sqlite3.Connection) -> tuple[int, str]:
        n = conn.execute("SELECT value FROM meta WHERE key=?", (META_HEAD,)).fetchone()["value"]
        h = conn.execute(
            "SELECT value FROM meta WHERE key=?", (META_HEAD_HASH,)
        ).fetchone()["value"]
        return int(n), h

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_block_header(self, number: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT header_json FROM blocks WHERE number=?", (number,)
            ).fetchone()
        return json.loads(row["header_json"]) if row else None

    def iter_blocks(self) -> Iterator[tuple[int, str, str, int]]:
        """按高度产出 (number, hash, parent_hash, tx_count)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT number, hash, parent_hash, tx_count FROM blocks ORDER BY number"
            ).fetchall()
        for r in rows:
            yield r["number"], r["hash"], r["parent_hash"], r["tx_count"]

    def iter_transactions(self) -> Iterator[dict[str, Any]]:
        """按高度、索引顺序产出全部已规整交易（离线回放输入）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tx_json FROM transactions ORDER BY block_number, tx_index"
            ).fetchall()
        for r in rows:
            yield json.loads(r["tx_json"])

    def get_receipt_by_tx_hash(self, tx_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT receipt_json FROM receipts WHERE tx_hash=?", (tx_hash,)
            ).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def get_transaction(self, tx_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT tx_json FROM transactions WHERE tx_hash=?", (tx_hash,)
            ).fetchone()
        return json.loads(row["tx_json"]) if row else None

    def receipts_for_block(self, number: int) -> list[Receipt]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT receipt_json FROM receipts WHERE block_number=? ORDER BY tx_index",
                (number,),
            ).fetchall()
        return [_receipt_from_dict(json.loads(r["receipt_json"])) for r in rows]

    def integrity_check(self) -> list[str]:
        """自检：链号、链尖指针、区块哈希与父子链接。返回问题列表（空=通过）。"""
        problems: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT number, hash, parent_hash, header_json FROM blocks ORDER BY number"
            ).fetchall()
            head_num, head_hash = self.head()
        expected_parent = GENESIS_PARENT
        for expected_num, r in enumerate(rows):
            header = json.loads(r["header_json"])
            block_hash = encoding.hexhash(header)
            if r["number"] != expected_num:
                problems.append(f"区块号序列断裂：期望 {expected_num}，实际 {r['number']}")
            if r["hash"] != block_hash:
                problems.append(f"区块 {r['number']} 哈希与头不一致")
            if r["parent_hash"] != expected_parent:
                problems.append(
                    f"区块 {r['number']} 父哈希不连续（期望 {expected_parent[:12]}…）"
                )
            expected_parent = r["hash"]
        if rows:
            last = rows[-1]
            if last["number"] != head_num or last["hash"] != head_hash:
                problems.append("链尖指针与最末区块不一致")
        elif (head_num, head_hash) != (-1, GENESIS_PARENT):
            problems.append("无区块但链尖指针非创世状态")
        return problems
