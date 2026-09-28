"""离线回放：从链式写前日志重建 UTXO 状态并复核状态根。

回放是确定性的：给定同一份日志，重建出的 UTXO 集与 state_root 必须与
索引存储中的值逐字节一致。任何不一致都被区分为：
- JOURNAL_CORRUPT    日志哈希链断裂/负载被改；
- STATE_ROOT_MISMATCH 日志完整，但重建状态根与库中记录不符（索引与日志分叉）。

用法：
    python -m tools.replay --db data/stackvm.db
    python -m tools.replay --rebuild-to /tmp/rebuilt.db --db data/stackvm.db
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from stackvm import hashes as H
from stackvm.errors import FailCode, VmFailure

_FRESH_SCHEMA = """
CREATE TABLE utxos (
    txid TEXT NOT NULL, vout INTEGER NOT NULL,
    value INTEGER NOT NULL, script TEXT NOT NULL,
    PRIMARY KEY (txid, vout));
CREATE TABLE transactions (txid TEXT PRIMARY KEY, height INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE journal (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, txid TEXT NOT NULL,
    payload TEXT NOT NULL, prev_hash TEXT NOT NULL, row_hash TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass
class ReplayReport:
    ok: bool
    code: FailCode = FailCode.OK
    detail: str = ""
    entries: int = 0
    applied: int = 0
    genesis_txid: str | None = None
    rebuilt_state_root: str = ""
    stored_state_root: str = ""
    utxo_count: int = 0
    utxos: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "ok": self.ok, "code": self.code.value, "detail": self.detail,
            "entries": self.entries, "applied": self.applied,
            "genesis_txid": self.genesis_txid,
            "rebuilt_state_root": self.rebuilt_state_root,
            "stored_state_root": self.stored_state_root,
            "utxo_count": self.utxo_count,
        }
        return d


def _hash_row(seq: int, kind: str, txid: str, payload: str, prev_hash: str) -> str:
    body = f"{seq}|{kind}|{txid}|{prev_hash}|{payload}".encode("utf-8")
    return H.hash256(body).hex()


def _read_journal(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM journal ORDER BY seq")]
    stored_root = conn.execute("SELECT value FROM meta WHERE key='state_root'").fetchone()
    conn.close()
    return rows, (stored_root[0] if stored_root else "")


def replay(db_path: str | Path, *, rebuild_to: str | Path | None = None) -> ReplayReport:
    db_path = Path(db_path)
    rows, stored_root = _read_journal(db_path)

    # 1) 校验哈希链
    prev_hash = "0" * 64
    for row in rows:
        expect = _hash_row(row["seq"], row["kind"], row["txid"],
                           row["payload"], row["prev_hash"])
        if row["prev_hash"] != prev_hash:
            return ReplayReport(False, FailCode.JOURNAL_CORRUPT,
                                f"seq={row['seq']} 前驱哈希断裂",
                                entries=len(rows), genesis_txid=(rows[0]["txid"] if rows else None),
                                stored_state_root=stored_root)
        if row["row_hash"] != expect:
            return ReplayReport(False, FailCode.JOURNAL_CORRUPT,
                                f"seq={row['seq']} 行负载哈希不匹配",
                                entries=len(rows), genesis_txid=(rows[0]["txid"] if rows else None),
                                stored_state_root=stored_root)
        prev_hash = row["row_hash"]

    # 2) 在全新内存/文件库中确定性重放负载
    target = Path(rebuild_to) if rebuild_to else None
    if target is not None:
        if target.exists():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        rec = sqlite3.connect(str(target))
    else:
        rec = sqlite3.connect(":memory:")
    rec.executescript(_FRESH_SCHEMA)

    applied = 0
    genesis_txid = None
    height = 0
    for row in rows:
        payload = json.loads(row["payload"])
        if row["kind"] == "GENESIS":
            genesis_txid = row["txid"]
            height = 1
            for o in payload["outputs"]:
                rec.execute("INSERT INTO utxos VALUES(?,?,?,?)",
                            (row["txid"], o["vout"], o["value"], o["script"]))
            rec.execute("INSERT INTO transactions VALUES(?,?,?)",
                        (row["txid"], 1, ""))
        elif row["kind"] == "APPLY":
            height += 1
            for ref in payload["spent"]:
                cur = rec.execute("SELECT 1 FROM utxos WHERE txid=? AND vout=?",
                                  (ref["txid"], ref["vout"])).fetchone()
                if cur is None:
                    rec.close()
                    return ReplayReport(
                        False, FailCode.JOURNAL_CORRUPT,
                        f"seq={row['seq']} 尝试花费不存在的 UTXO "
                        f"{ref['txid']}:{ref['vout']}（日志负载自相矛盾）",
                        entries=len(rows), applied=applied, genesis_txid=genesis_txid,
                        stored_state_root=stored_root)
                rec.execute("DELETE FROM utxos WHERE txid=? AND vout=?",
                            (ref["txid"], ref["vout"]))
            for o in payload["created"]:
                rec.execute("INSERT INTO utxos VALUES(?,?,?,?)",
                            (row["txid"], o["vout"], o["value"], o["script"]))
            rec.execute("INSERT INTO transactions VALUES(?,?,?)",
                        (row["txid"], height, ""))
            applied += 1
        else:
            rec.close()
            return ReplayReport(False, FailCode.JOURNAL_CORRUPT,
                                f"未知日志类型 {row['kind']} @ seq={row['seq']}",
                                entries=len(rows), stored_state_root=stored_root)

    rec.commit()
    utxo_rows = rec.execute(
        "SELECT txid,vout,value,script FROM utxos ORDER BY txid,vout").fetchall()
    concat = b"".join(
        r[0].encode() + int(r[1]).to_bytes(4, "little")
        + int(r[2]).to_bytes(8, "little") + r[3].encode() for r in utxo_rows)
    rebuilt_root = H.hash256(concat).hex()
    rec.close()

    report = ReplayReport(
        ok=(rebuilt_root == stored_root),
        entries=len(rows), applied=applied, genesis_txid=genesis_txid,
        rebuilt_state_root=rebuilt_root, stored_state_root=stored_root,
        utxo_count=len(utxo_rows),
        utxos=[{"txid": r[0], "vout": r[1], "value": r[2], "script": r[3]}
               for r in utxo_rows],
    )
    if not report.ok:
        report.code = FailCode.STATE_ROOT_MISMATCH
        report.detail = (f"日志完整但重建状态根 {rebuilt_root} 与库中 {stored_root} "
                         f"不一致：索引存储与日志分叉")
    else:
        report.detail = f"回放一致：{applied} 笔应用交易，{len(utxo_rows)} 个 UTXO"
    return report
