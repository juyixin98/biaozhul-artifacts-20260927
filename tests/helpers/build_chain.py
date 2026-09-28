"""跨进程重放的「进程 A」：构建固定链并导出收据。

由 tests/test_replay_cross_process.py 在独立解释器中调用。
输出：stdout 打印一行汇总 JSON；收据清单写到 --out 文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from teachchain import fixtures  # noqa: E402
from teachchain.replay import replay_store  # noqa: E402
from teachchain.service import ChainService  # noqa: E402
from teachchain.storage import IndexStore  # noqa: E402


def addr_of(signer, nonce: int) -> str:
    return "0x" + hashlib.sha256(
        f"{signer.address}:{nonce}".encode()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if os.path.exists(args.db):
        os.remove(args.db)
    for suffix in ("-wal", "-shm"):
        p = args.db + suffix
        if os.path.exists(p):
            os.remove(p)

    store = IndexStore(args.db)
    svc = ChainService(store)
    alice = fixtures.signer("alice")
    svc.seed(alice.address, 10_000_000)

    def env(*a, **kw):
        return fixtures.envelope(alice, *a, **kw)

    # 1 部署 counter
    svc.submit(env("deploy", nonce=0, gas_limit=500_000,
                   code=fixtures.counter_code()))
    counter = addr_of(alice, 0)
    # 2 调用 +10
    svc.submit(env("invoke", nonce=1, gas_limit=500_000, to=counter, words=[10]))
    # 3 临界 gas -> out_of_gas
    svc.submit(env("invoke", nonce=2, gas_limit=21_004, to=counter, words=[5]))
    # 4 部署 write-then-halt
    svc.submit(env("deploy", nonce=3, gas_limit=500_000,
                   code=fixtures.write_then_halt_code()))
    halt = addr_of(alice, 3)
    # 5 调用 -> invalid_instruction
    svc.submit(env("invoke", nonce=4, gas_limit=200_000, to=halt))
    # 6 部署 write-then-revert
    svc.submit(env("deploy", nonce=5, gas_limit=500_000,
                   code=fixtures.write_then_revert_code()))
    rev = addr_of(alice, 5)
    # 7 调用 -> revert
    svc.submit(env("invoke", nonce=6, gas_limit=200_000, to=rev))
    # 8 部署 caller
    svc.submit(env("deploy", nonce=7, gas_limit=500_000,
                   code=fixtures.caller_code()))
    caller = addr_of(alice, 7)
    # 9 嵌套调用（子=write-then-halt），父仍成功
    svc.submit(env("invoke", nonce=8, gas_limit=500_000, to=caller,
                   words=[int(halt, 16)]))

    receipts = store.list_receipts(limit=1000)
    receipts = list(reversed(receipts))  # DESC -> ASC
    with open(args.out, "w") as fh:
        json.dump(receipts, fh, sort_keys=True)

    # 构建后立即在同一进程自校验一次（跨进程校验由测试进程 B 再做）
    report = replay_store(store, stop_on_first=False).summary()
    summary = {
        "tx_total": report["tx_total"],
        "final_state_root": report["final_state_root"],
    }
    print(json.dumps(summary, sort_keys=True))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
