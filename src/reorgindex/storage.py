"""SQLite 存储层。

表分四组：
1. 区块与连接：blocks（父哈希、高度、累计权重）、tx_contents、tx_inclusions
2. 派生索引（可撤回）：derived_contributions（tx_id 唯一，存在即"有效贡献"）、
   derived_balances
3. 悬挂池：pending_blocks（父区块未知时先挂起）
4. 审计：diag_events（接受/拒绝/无法判定的原因）、reorg_events（回滚区间）

写入由内核在单个 IMMEDIATE 事务中完成，切换"先撤旧后加新"，
事务失败整体回滚——任何中途异常都不会留下混合链版本的派生结果。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    block_hash       TEXT PRIMARY KEY,
    height           INTEGER NOT NULL,
    prev_hash        TEXT NOT NULL,
    weight           INTEGER NOT NULL,
    cumulative_weight INTEGER NOT NULL,
    proposer         TEXT NOT NULL,
    merkle_root      TEXT NOT NULL,
    received_seq     INTEGER NOT NULL,
    raw              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_prev   ON blocks(prev_hash);
CREATE INDEX IF NOT EXISTS idx_blocks_height ON blocks(height);

CREATE TABLE IF NOT EXISTS tx_contents (
    tx_id          TEXT PRIMARY KEY,
    sender_pubkey  TEXT NOT NULL,
    recipient      TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    nonce          INTEGER NOT NULL,
    memo           TEXT NOT NULL DEFAULT '',
    signature      TEXT NOT NULL,
    first_seen_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tx_inclusions (
    block_hash TEXT NOT NULL REFERENCES blocks(block_hash),
    tx_id      TEXT NOT NULL,
    position   INTEGER NOT NULL,
    seen_seq   INTEGER NOT NULL,
    PRIMARY KEY (block_hash, position)
);
CREATE INDEX IF NOT EXISTS idx_inclusions_tx ON tx_inclusions(tx_id);

CREATE TABLE IF NOT EXISTS derived_contributions (
    tx_id      TEXT PRIMARY KEY,        -- 唯一：跨分叉/重复包含都只可能有一个有效贡献
    block_hash TEXT NOT NULL,
    position   INTEGER NOT NULL,
    recipient  TEXT NOT NULL,
    amount     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_balances (
    address TEXT PRIMARY KEY,
    balance INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_blocks (
    block_hash   TEXT PRIMARY KEY,
    parent_hash  TEXT NOT NULL,
    received_seq INTEGER NOT NULL,
    raw          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_parent ON pending_blocks(parent_hash);

CREATE TABLE IF NOT EXISTS diag_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL,
    request_id TEXT,
    event      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reorg_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id         TEXT,
    old_tip            TEXT NOT NULL,
    new_tip            TEXT NOT NULL,
    disconnected       TEXT NOT NULL,   -- 撤掉的区块哈希（高度降序）
    connected          TEXT NOT NULL,   -- 新加的区块哈希（高度升序）
    rollback_from_height INTEGER,
    rollback_to_height   INTEGER,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Storage:
    def __init__(self, db_path: str = ":memory:"):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 自加锁：FastAPI 线程池下串行化写入。
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._seq = 0
        self._configure()
        self._init_schema()

    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- 事务

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """单个 IMMEDIATE 事务；异常自动回滚。"""

        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def bootstrap_seq(self) -> None:
        """打开已有库后把内存序号对齐到磁盘最大值。"""

        row = self._conn.execute(
            "SELECT COALESCE(MAX(received_seq), 0) FROM ("
            "  SELECT MAX(received_seq) AS received_seq FROM blocks"
            "  UNION ALL SELECT MAX(received_seq) FROM pending_blocks"
            ")"
        ).fetchone()
        self._seq = max(self._seq, row[0])

    # ------------------------------------------------------------- 元信息/链尖

    def get_tip(self) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key='tip_hash'").fetchone()
        return row[0] if row else None

    def set_tip(self, conn: sqlite3.Connection, tip_hash: str | None) -> None:
        if tip_hash is None:
            conn.execute("DELETE FROM meta WHERE key='tip_hash'")
        else:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('tip_hash', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (tip_hash,),
            )

    # ------------------------------------------------------------- 区块

    def block_exists(self, block_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM blocks WHERE block_hash=? UNION ALL SELECT 1 FROM pending_blocks WHERE block_hash=?",
            (block_hash, block_hash),
        ).fetchone()
        return row is not None

    def get_block(self, block_hash: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM blocks WHERE block_hash=?", (block_hash,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_block_raw(self, block_hash: str) -> dict | None:
        row = self._conn.execute("SELECT raw FROM blocks WHERE block_hash=?", (block_hash,)).fetchone()
        return json.loads(row["raw"]) if row else None

    def insert_block(
        self,
        conn: sqlite3.Connection,
        block: dict,
        received_seq: int,
        cumulative_weight: int,
    ) -> None:
        header = block["header"]
        conn.execute(
            "INSERT INTO blocks(block_hash, height, prev_hash, weight, cumulative_weight,"
            " proposer, merkle_root, received_seq, raw) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                header["block_hash"],
                header["height"],
                header["prev_hash"],
                header["weight"],
                cumulative_weight,
                header.get("proposer", ""),
                header["merkle_root"],
                received_seq,
                json.dumps(block, ensure_ascii=False, sort_keys=True),
            ),
        )
        for pos, tx in enumerate(block["txs"]):
            conn.execute(
                "INSERT INTO tx_inclusions(block_hash, tx_id, position, seen_seq) VALUES(?,?,?,?)",
                (header["block_hash"], tx["tx_id"], pos, received_seq),
            )
            # 交易内容以首次见到为准保存（不同分支上的同 tx_id 必然同体，因为 tx_id 是哈希）。
            conn.execute(
                "INSERT INTO tx_contents(tx_id, sender_pubkey, recipient, amount, nonce, memo,"
                " signature, first_seen_seq) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(tx_id) DO NOTHING",
                (
                    tx["tx_id"],
                    tx["sender_pubkey"],
                    tx["recipient"],
                    tx["amount"],
                    tx["nonce"],
                    tx.get("memo", ""),
                    tx["signature"],
                    received_seq,
                ),
            )

    def all_block_hashes(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT block_hash FROM blocks").fetchall()]

    def genesis_exists(self) -> bool:
        return self._conn.execute("SELECT 1 FROM blocks WHERE height=0 LIMIT 1").fetchone() is not None

    def all_stored_blocks_raw(self) -> list[dict]:
        """供全量重建：返回库中每个区块的原始 JSON（不限于权威链）。"""

        return [json.loads(r[0]) for r in self._conn.execute("SELECT raw FROM blocks").fetchall()]

    # ------------------------------------------------------------- 悬挂池

    def add_pending(self, conn: sqlite3.Connection, block: dict, received_seq: int) -> None:
        header = block["header"]
        conn.execute(
            "INSERT INTO pending_blocks(block_hash, parent_hash, received_seq, raw) VALUES(?,?,?,?)",
            (header["block_hash"], header["prev_hash"], received_seq, json.dumps(block, sort_keys=True)),
        )

    def take_pending_children(self, conn: sqlite3.Connection, parent_hash: str) -> list[dict]:
        """取出并移除父哈希等于 parent_hash 的悬挂块，按接收顺序（同高度时稳定）。"""

        rows = conn.execute(
            "SELECT raw FROM pending_blocks WHERE parent_hash=? ORDER BY received_seq, rowid",
            (parent_hash,),
        ).fetchall()
        conn.execute("DELETE FROM pending_blocks WHERE parent_hash=?", (parent_hash,))
        return [json.loads(r["raw"]) for r in rows]

    def pending_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM pending_blocks").fetchone()[0]

    def all_pending_raw(self) -> list[dict]:
        return [json.loads(r[0]) for r in self._conn.execute(
            "SELECT raw FROM pending_blocks ORDER BY received_seq").fetchall()]

    def delete_pending(self, conn: sqlite3.Connection, block_hash: str) -> None:
        conn.execute("DELETE FROM pending_blocks WHERE block_hash=?", (block_hash,))

    # ------------------------------------------------------------- 派生索引

    def contribution_exists(self, conn: sqlite3.Connection, tx_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM derived_contributions WHERE tx_id=?", (tx_id,)
        ).fetchone() is not None

    def apply_block(self, conn: sqlite3.Connection, block_hash: str, txs: list[dict]) -> int:
        """把一个区块的交易贡献加入派生表，返回新增贡献条数。

        同一 tx_id 已在当前链贡献过（同链重复交易/跨分叉已在共同祖先贡献）
        则跳过——任何情况下一笔交易最多产生一个有效贡献。
        """

        added = 0
        for pos, tx in enumerate(txs):
            if self.contribution_exists(conn, tx["tx_id"]):
                continue
            conn.execute(
                "INSERT INTO derived_contributions(tx_id, block_hash, position, recipient, amount)"
                " VALUES(?,?,?,?,?)",
                (tx["tx_id"], block_hash, pos, tx["recipient"], tx["amount"]),
            )
            conn.execute(
                "INSERT INTO derived_balances(address, balance) VALUES(?, ?) "
                "ON CONFLICT(address) DO UPDATE SET balance = balance + excluded.balance",
                (tx["recipient"], tx["amount"]),
            )
            added += 1
        return added

    def unapply_block(self, conn: sqlite3.Connection, block_hash: str) -> int:
        """撤回一个区块对派生表的贡献，返回撤回条数。仅撤该块实际贡献的交易。"""

        rows = conn.execute(
            "SELECT tx_id, recipient, amount FROM derived_contributions WHERE block_hash=?",
            (block_hash,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE derived_balances SET balance = balance - ? WHERE address=?",
                (row["amount"], row["recipient"]),
            )
            conn.execute("DELETE FROM derived_balances WHERE address=? AND balance=0", (row["recipient"],))
            conn.execute("DELETE FROM derived_contributions WHERE tx_id=?", (row["tx_id"],))
        return len(rows)

    def get_balance(self, address: str) -> int:
        row = self._conn.execute(
            "SELECT balance FROM derived_balances WHERE address=?", (address,)
        ).fetchone()
        return row[0] if row else 0

    def all_balances(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._conn.execute(
            "SELECT address, balance FROM derived_balances").fetchall()}

    def contribution_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM derived_contributions").fetchone()[0]

    def contributing_block_hashes(self) -> set[str]:
        return {r[0] for r in self._conn.execute(
            "SELECT DISTINCT block_hash FROM derived_contributions").fetchall()}

    def clear_derived(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM derived_contributions")
        conn.execute("DELETE FROM derived_balances")

    # ------------------------------------------------------------- 审计

    def log_diag(self, conn: sqlite3.Connection | None, event_id: str, request_id: str | None,
                 event: str, payload: dict[str, Any]) -> None:
        target = conn or self._conn
        target.execute(
            "INSERT INTO diag_events(event_id, request_id, event, payload) VALUES(?,?,?,?)",
            (event_id, request_id, event, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def recent_diag(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT event_id, request_id, event, payload, created_at FROM diag_events"
            " ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    def log_reorg(self, conn: sqlite3.Connection, request_id: str | None, old_tip: str, new_tip: str,
                  disconnected: list[str], connected: list[str],
                  rollback_from: int | None, rollback_to: int | None) -> None:
        conn.execute(
            "INSERT INTO reorg_events(request_id, old_tip, new_tip, disconnected, connected,"
            " rollback_from_height, rollback_to_height) VALUES(?,?,?,?,?,?,?)",
            (request_id, old_tip, new_tip, json.dumps(disconnected), json.dumps(connected),
             rollback_from, rollback_to),
        )

    def recent_reorgs(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT request_id, old_tip, new_tip, disconnected, connected,"
            " rollback_from_height, rollback_to_height, created_at FROM reorg_events"
            " ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["disconnected"] = json.loads(item["disconnected"])
            item["connected"] = json.loads(item["connected"])
            out.append(item)
        return out
