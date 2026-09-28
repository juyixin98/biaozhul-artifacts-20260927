"""SQLite 索引存储。

把链状态内核产生的区块/回执/账户快照持久化，并建立可查询索引：
  - 按交易哈希取回执；
  - 按选择器/方法名检索交易；
  - 按账户检索其参与的转账；
  - 区块号、状态根、区块哈希的一一对应。

全部语句使用参数化查询，杜绝 SQL 注入。schema 带版本号，支持初始化校验。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..kernel import Block, Receipt

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    number      INTEGER PRIMARY KEY,
    parent_hash TEXT NOT NULL,
    state_root  TEXT NOT NULL,
    block_hash  TEXT NOT NULL,
    tx_count    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_hash        TEXT PRIMARY KEY,
    block_number   INTEGER NOT NULL,
    tx_index       INTEGER NOT NULL,
    selector       TEXT NOT NULL,
    signature      TEXT NOT NULL,
    status         TEXT NOT NULL,
    error_category TEXT,
    error_message  TEXT,
    from_account   TEXT,
    to_account     TEXT,
    amount         TEXT,
    calldata       TEXT NOT NULL,
    FOREIGN KEY (block_number) REFERENCES blocks(number)
);

CREATE INDEX IF NOT EXISTS idx_tx_block    ON transactions(block_number);
CREATE INDEX IF NOT EXISTS idx_tx_selector ON transactions(selector);
CREATE INDEX IF NOT EXISTS idx_tx_status   ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_tx_from     ON transactions(from_account);
CREATE INDEX IF NOT EXISTS idx_tx_to       ON transactions(to_account);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    block_number INTEGER NOT NULL,
    address      TEXT NOT NULL,
    balance      TEXT NOT NULL,
    nonce        INTEGER NOT NULL,
    note         TEXT NOT NULL,
    UNIQUE(block_number, address)
);
CREATE INDEX IF NOT EXISTS idx_snap_acct ON account_snapshots(address, block_number);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        # FastAPI 可能在工作线程使用同一连接；本地单进程演示后端，
        # 用一把锁串行化写入以保证安全。
        import threading
        self._wlock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(_SCHEMA)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
        # 校验既有库版本
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None or int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"不兼容的存储 schema 版本：期望 {SCHEMA_VERSION}，实际 "
                f"{row['value'] if row else '缺失'}"
            )

    @contextmanager
    def transaction(self):
        with self._wlock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def save_block(self, block: Block, calldatas: list[bytes], accounts: dict) -> None:
        """原子地写入一个区块、其交易回执与该区块后的账户快照。"""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO blocks(number,parent_hash,state_root,block_hash,tx_count)"
                " VALUES(?,?,?,?,?)",
                (
                    block.number,
                    "0x" + block.parent_hash.hex(),
                    "0x" + block.state_root.hex(),
                    "0x" + block.block_hash.hex(),
                    len(block.receipts),
                ),
            )
            for receipt, cd in zip(block.receipts, calldatas):
                selector = "0x" + cd[:4].hex() if len(cd) >= 4 else "0x"
                conn.execute(
                    "INSERT INTO transactions("
                    "tx_hash,block_number,tx_index,selector,signature,status,"
                    "error_category,error_message,from_account,to_account,amount,calldata)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt.tx_hash,
                        receipt.block_number,
                        receipt.index,
                        selector,
                        receipt.signature,
                        receipt.status,
                        receipt.error_category,
                        receipt.error_message,
                        receipt.from_account,
                        receipt.to_account,
                        str(receipt.amount) if receipt.amount is not None else None,
                        "0x" + cd.hex(),
                    ),
                )
            for addr, acct in accounts.items():
                conn.execute(
                    "INSERT INTO account_snapshots(block_number,address,balance,nonce,note)"
                    " VALUES(?,?,?,?,?)",
                    (
                        block.number,
                        "0x" + addr.hex(),
                        str(acct.balance),
                        acct.nonce,
                        acct.note,
                    ),
                )

    # ---- 查询 ----
    def get_transaction(self, tx_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM transactions WHERE tx_hash=?", (tx_hash,)
        ).fetchone()
        return dict(row) if row else None

    def get_block(self, number: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM blocks WHERE number=?", (number,)
        ).fetchone()
        return dict(row) if row else None

    def find_by_selector(self, selector_hex: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT tx_hash,block_number,signature,status,error_category "
            "FROM transactions WHERE selector=? ORDER BY block_number,tx_index",
            (selector_hex,),
        ).fetchall()
        return [dict(r) for r in rows]

    def transactions_for_account(self, address: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT tx_hash,block_number,signature,status,from_account,to_account,amount "
            "FROM transactions WHERE from_account=? OR to_account=? "
            "ORDER BY block_number,tx_index",
            (address, address),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_snapshot(self, address: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM account_snapshots WHERE address=? "
            "ORDER BY block_number DESC LIMIT 1",
            (address,),
        ).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        c = self.conn
        blocks = c.execute("SELECT COUNT(*) n FROM blocks").fetchone()["n"]
        txs = c.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
        reverted = c.execute(
            "SELECT COUNT(*) n FROM transactions WHERE status='reverted'"
        ).fetchone()["n"]
        return {"blocks": blocks, "transactions": txs, "reverted": reverted}

    def close(self) -> None:
        self.conn.close()
