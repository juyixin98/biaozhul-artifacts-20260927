#!/usr/bin/env python3
"""不依赖服务的直接内核调用示例（同进程，内存 SQLite）。

运行：PYTHONPATH=src .venv/bin/python examples/direct_kernel.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from rsv.chain import ChainKernel
from rsv.config import load_settings
from rsv.replay import replay_bundle
from rsv.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rsv-example-"))
    settings = load_settings(
        sqlite_path=str(tmp / "demo.sqlite3"), runs_dir=str(tmp / "runs")
    )
    kernel = ChainKernel(settings, SQLiteStore(settings.sqlite_path))

    genesis = json.loads((ROOT / "fixtures/genesis.json").read_text())
    print("bootstrap:", kernel.bootstrap_from_dict(genesis))

    happy = json.loads((ROOT / "fixtures/bundles/happy_path.json").read_text())
    for i, tx in enumerate(happy["transactions"], 1):
        rep = kernel.verify_transaction_dict(tx, persist=True, kind="submit")
        print(f"happy tx#{i}: accepted={rep.accepted} steps={rep.steps_used} "
              f"run={rep.run_id}")

    failures = json.loads((ROOT / "fixtures/bundles/failure_catalog.json").read_text())
    print("\n-- 失败目录（每条都不改状态）--")
    for i, tx in enumerate(failures["transactions"], 1):
        rep = kernel.verify_transaction_dict(tx, persist=False, kind="verify")
        f = rep.failure
        print(f"fail tx#{i}: [{f['category']}] {f['code']} -- {f['detail'][:60]}")

    print("\n-- 离线 replay（独立内存状态）--")
    res = replay_bundle(failures, runs_dir=str(tmp / "replay"))
    for r in res.results:
        if "failure" in r:
            print(f"  seq{r['seq']}: {r['failure']['code']}")
    print("final root:", res.final_state_root)


if __name__ == "__main__":
    main()
