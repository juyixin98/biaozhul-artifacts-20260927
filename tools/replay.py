"""离线回放 CLI：校验链式写前日志并从日志重建 UTXO 状态根。

    python -m tools.replay --db data/stackvm.db
    python -m tools.replay --db data/stackvm.db --rebuild-to /tmp/rebuilt.db --json
"""
from __future__ import annotations

import argparse
import json

from chain.replay import replay
from stackvm.config import load_settings


def main() -> int:
    ap = argparse.ArgumentParser(description="离线回放 StackVM 链式日志")
    ap.add_argument("--db", default=None, help="SQLite 库路径（默认取配置 storage.db_path）")
    ap.add_argument("--rebuild-to", default=None, help="把重建库落盘到该路径")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = ap.parse_args()

    settings = load_settings()
    db = args.db or settings.abspath(settings.storage.db_path)
    report = replay(db, rebuild_to=args.rebuild_to)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"回放结果      : {'OK' if report.ok else 'FAIL'}")
        if not report.ok:
            print(f"失败分类      : {report.code.value}")
        print(f"判定理由      : {report.detail}")
        print(f"日志条目      : {report.entries}（其中 APPLY {report.applied}）")
        print(f"genesis txid  : {report.genesis_txid}")
        print(f"存储状态根    : {report.stored_state_root}")
        print(f"重建状态根    : {report.rebuilt_state_root}")
        print(f"UTXO 数量     : {report.utxo_count}")
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
