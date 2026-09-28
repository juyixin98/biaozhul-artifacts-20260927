#!/usr/bin/env python3
"""把 SQLite 版本链与流水导出为 JSONL（供离线回放，无需接触原库）。

两类记录：
  {"record":"checkpoint","version":..,"root":..,"parent_root":..,"batch_id":..,"signature":..}
  {"record":"journal","version":..,"batch_id":..,"seq":..,"key":..,"value":..}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.storage.sqlite_store import SqliteStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/smt.db")
    ap.add_argument("--out", default="data/journal_export.jsonl")
    args = ap.parse_args()

    store = SqliteStore(args.db)
    lines: list[str] = []
    for cp in store.all_checkpoints():
        lines.append(json.dumps({
            "record": "checkpoint",
            "version": cp["version"],
            "root": cp["root"].hex(),
            "parent_root": cp["parent_root"].hex() if cp["parent_root"] else None,
            "batch_id": cp["batch_id"],
            "signature": cp["signature"].hex(),
        }, sort_keys=True))
    for row in store.all_journal():
        lines.append(json.dumps({
            "record": "journal",
            "version": row["version"],
            "batch_id": row["batch_id"],
            "seq": row["seq"],
            "key": row["nkey"].hex(),
            "value": None if row["nvalue"] is None else row["nvalue"].hex(),
        }, sort_keys=True))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"exported {len(lines)} records -> {args.out}")
    store.close()


if __name__ == "__main__":
    main()
