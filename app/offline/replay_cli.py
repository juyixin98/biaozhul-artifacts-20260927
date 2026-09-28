"""python -m app.offline.replay —— 离线回放命令行。

用法：
  python -m app.offline.replay --journal data/journal_export.jsonl \
      --public-key configs/dev_signing_key.pem.pub [--key-len 32 --depth 256]
  python -m app.offline.replay --db data/smt.db --public-key <受信公钥>
      （后者额外对活动库存活键做交叉出证核验）

退出码：ACCEPT=0，REJECT=2，INCONCLUSIVE=3。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.coding.params import TreeParams
from app.core.smt import SparseMerkleTree
from app.offline.replay import (
    cross_check_live_store,
    load_jsonl,
    replay_jsonl,
)
from app.storage.sqlite_store import SqliteStore

_EXIT = {"ACCEPT": 0, "REJECT": 2, "INCONCLUSIVE": 3}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="稀疏 Merkle 状态离线回放/核验")
    ap.add_argument("--journal", help="JSONL 流水（export_journal.py 产物）")
    ap.add_argument("--db", help="活动 SQLite 库（可选，提供则追加活动状态交叉核验）")
    ap.add_argument("--public-key", required=True, help="受信 Ed25519 公钥 PEM（带外信任）")
    ap.add_argument("--key-len", type=int, default=32)
    ap.add_argument("--depth", type=int, default=256)
    args = ap.parse_args(argv)

    if not args.journal and not args.db:
        ap.error("至少提供 --journal 或 --db")

    pub_pem = Path(args.public_key).read_bytes()
    params = TreeParams(args.key_len, args.depth)

    if args.journal:
        records = load_jsonl(args.journal)
    else:
        records = _records_from_db(args.db)

    report = replay_jsonl(records, pub_pem, params)

    if args.db and report.decision.value == "ACCEPT":
        store = SqliteStore(args.db)
        try:
            tree = SparseMerkleTree(store, params)
            live = store.live_keys()
            root = store.latest_checkpoint()["root"]
            report = cross_check_live_store(report, tree, live, root)
        finally:
            store.close()

    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return _EXIT[report.decision.value]


def _records_from_db(db_path: str) -> list[dict]:
    store = SqliteStore(db_path)
    try:
        records: list[dict] = []
        for cp in store.all_checkpoints():
            records.append({
                "record": "checkpoint",
                "version": cp["version"],
                "root": cp["root"].hex(),
                "parent_root": cp["parent_root"].hex() if cp["parent_root"] else None,
                "batch_id": cp["batch_id"],
                "signature": cp["signature"].hex(),
            })
        for row in store.all_journal():
            records.append({
                "record": "journal",
                "version": row["version"],
                "batch_id": row["batch_id"],
                "seq": row["seq"],
                "key": row["nkey"].hex(),
                "value": None if row["nvalue"] is None else row["nvalue"].hex(),
            })
        return records
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
