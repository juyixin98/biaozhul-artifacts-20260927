"""SQLite 索引存储。

- utxos：当前 UTXO 集（索引，可由日志重建）；
- transactions：已接受交易（txid 唯一，拒绝重复入账）；
- journal：链式写前日志，每行包含前一行的链式哈希 prev_hash，断链 → JOURNAL_CORRUPT；
- meta：state_root（UTXO 集的 HASH256 摘要）、chain_height 等。

提交路径 `apply_transaction` 在**单个 SQLite 事务**内完成：
花费旧 UTXO → 插入新 UTXO → 登记交易 → 追加日志 → 更新状态根。
验证失败时上层根本不会进入本模块，因此“只返回失败分类，不执行转账”有事务保证。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from stackvm import hashes as H
from stackvm.errors import FailCode, VmFailure
from stackvm.transaction import Transaction, txid_of

GENESIS_TXID = "00" * 32  # 零输入铸币交易的父引用占位（真实 genesis tx 有自己的 txid）
GENESIS_PREV = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS utxos (
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    value INTEGER NOT NULL,
    script TEXT NOT NULL,
    PRIMARY KEY (txid, vout)
);
CREATE TABLE IF NOT EXISTS transactions (
    txid TEXT PRIMARY KEY,
    height INTEGER NOT NULL,
    body TEXT NOT NULL,
    accepted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    txid TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：FastAPI 端点线程与测试客户端主线程可能不同；
        # 提交路径由 BEGIN IMMEDIATE + 单连接串行化保护。
        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self._ensure_meta()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------ meta ------------------------------

    def _ensure_meta(self) -> None:
        self.conn.executescript(_SCHEMA)
        if self._get_meta("state_root") is None:
            self._set_meta("state_root", H.hash256(b"empty-utxo-set").hex())
            self._set_meta("chain_height", "0")

    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @property
    def state_root(self) -> str:
        return self._get_meta("state_root") or ""

    @property
    def chain_height(self) -> int:
        return int(self._get_meta("chain_height") or "0")

    # ------------------------------ 查询 ------------------------------

    def get_utxo(self, txid: str, vout: int) -> tuple[int, str] | None:
        row = self.conn.execute(
            "SELECT value, script FROM utxos WHERE txid=? AND vout=?",
            (txid, vout),
        ).fetchone()
        return (row["value"], row["script"]) if row else None

    def has_transaction(self, txid: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM transactions WHERE txid=?", (txid,)
        ).fetchone() is not None

    def list_utxos(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT txid, vout, value, script FROM utxos ORDER BY txid, vout"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------ 状态根 ------------------------------

    def compute_state_root(self) -> str:
        rows = self.conn.execute(
            "SELECT txid, vout, value, script FROM utxos ORDER BY txid, vout"
        ).fetchall()
        concat = b"".join(
            r["txid"].encode() + int(r["vout"]).to_bytes(4, "little")
            + int(r["value"]).to_bytes(8, "little") + r["script"].encode()
            for r in rows
        )
        return H.hash256(concat).hex()

    # ------------------------------ 日志 ------------------------------

    @staticmethod
    def _row_hash(seq: int, kind: str, txid: str, payload: str, prev_hash: str) -> str:
        body = f"{seq}|{kind}|{txid}|{prev_hash}|{payload}".encode("utf-8")
        return H.hash256(body).hex()

    def _append_journal(self, kind: str, txid: str, payload: dict) -> str:
        seq = self.conn.execute("SELECT COALESCE(MAX(seq), -1) + 1 AS s FROM journal").fetchone()["s"]
        prev = self.conn.execute("SELECT row_hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = prev["row_hash"] if prev else "0" * 64
        payload_text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        row_hash = self._row_hash(seq, kind, txid, payload_text, prev_hash)
        self.conn.execute(
            "INSERT INTO journal(seq,kind,txid,payload,prev_hash,row_hash) "
            "VALUES(?,?,?,?,?,?)",
            (seq, kind, txid, payload_text, prev_hash, row_hash),
        )
        return row_hash

    def verify_journal(self) -> None:
        """校验链式日志完整性；断链 → JOURNAL_CORRUPT。"""
        prev_hash = "0" * 64
        for row in self.conn.execute("SELECT * FROM journal ORDER BY seq"):
            expect = self._row_hash(row["seq"], row["kind"], row["txid"],
                                    row["payload"], row["prev_hash"])
            if row["prev_hash"] != prev_hash:
                raise VmFailure(
                    FailCode.JOURNAL_CORRUPT,
                    f"日志 seq={row['seq']} 前驱哈希不匹配（可能被截断/改写）",
                )
            if row["row_hash"] != expect:
                raise VmFailure(
                    FailCode.JOURNAL_CORRUPT,
                    f"日志 seq={row['seq']} 行哈希不匹配（负载被篡改）",
                )
            prev_hash = row["row_hash"]

    def list_journal(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT seq,kind,txid,payload,prev_hash,row_hash FROM journal ORDER BY seq")]

    # ------------------------------ 提交 ------------------------------

    def apply_transaction(self, tx: Transaction, spent: list[dict],
                          *, state_root_note: str = "") -> str:
        tid = txid_of(tx)
        body = tx.to_json()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.has_transaction(tid):
                raise VmFailure(FailCode.TX_ALREADY_ACCEPTED, f"交易 {tid} 已接受")
            # 双花在同事务内二次确认
            for ref in spent:
                if self.get_utxo(ref["txid"], ref["vout"]) is None:
                    raise VmFailure(
                        FailCode.UTXO_MISSING,
                        f"{ref['txid']}:{ref['vout']} 已被花费或不存在",
                    )
            for ref in spent:
                self.conn.execute(
                    "DELETE FROM utxos WHERE txid=? AND vout=?",
                    (ref["txid"], ref["vout"]),
                )
            for vout, out in enumerate(tx.outputs):
                self.conn.execute(
                    "INSERT INTO utxos(txid,vout,value,script) VALUES(?,?,?,?)",
                    (tid, vout, out.value, out.script),
                )
            height = self.chain_height + 1
            self.conn.execute(
                "INSERT INTO transactions(txid,height,body) VALUES(?,?,?)",
                (tid, height, body),
            )
            self._append_journal("APPLY", tid, {
                "spent": spent,
                "created": [{"vout": i, "value": o.value, "script": o.script}
                            for i, o in enumerate(tx.outputs)],
                "sighash": state_root_note,
            })
            root = self.compute_state_root()
            self._set_meta("state_root", root)
            self._set_meta("chain_height", str(height))
            self.conn.commit()
            return root
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------ genesis 引导 ------------------------------

    def bootstrap_genesis(self, genesis: dict, mint_cap: int) -> str:
        """从零输入铸币交易引导测试链。仅允许在空库时调用一次。

        genesis 结构：{"tx": <wire tx>, "outputs": [...]} 或直接为 wire tx。
        铸币总量超过上限 → TX_MALFORMED；库非空 → TX_ALREADY_ACCEPTED。
        """
        from stackvm.transaction import transaction_from_dict

        wire = genesis["tx"] if isinstance(genesis, dict) and "tx" in genesis else genesis
        tx = transaction_from_dict(wire, allow_empty_inputs=True)
        tid = txid_of(tx)
        if tx.inputs:
            raise VmFailure(FailCode.TX_MALFORMED, "genesis 必须是零输入铸币交易")
        total = sum(o.value for o in tx.outputs)
        if total > mint_cap:
            raise VmFailure(FailCode.TX_MALFORMED,
                            f"genesis 铸币 {total} 超过上限 {mint_cap}")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.chain_height != 0 or self.has_transaction(tid):
                raise VmFailure(FailCode.TX_ALREADY_ACCEPTED, "genesis 已引导，禁止重复铸币")
            for vout, out in enumerate(tx.outputs):
                self.conn.execute(
                    "INSERT INTO utxos(txid,vout,value,script) VALUES(?,?,?,?)",
                    (tid, vout, out.value, out.script),
                )
            self.conn.execute(
                "INSERT INTO transactions(txid,height,body) VALUES(?,?,?)",
                (tid, 1, tx.to_json()),
            )
            self._append_journal("GENESIS", tid, {
                "outputs": [{"vout": i, "value": o.value, "script": o.script}
                            for i, o in enumerate(tx.outputs)],
                "minted": total,
            })
            root = self.compute_state_root()
            self._set_meta("state_root", root)
            self._set_meta("chain_height", "1")
            self.conn.commit()
            return root
        except Exception:
            self.conn.rollback()
            raise
