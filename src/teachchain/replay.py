"""离线回放：从持久化的信封独立重放整条链，逐笔核对收据与状态根。

这是“参考答案不由被测核心自身生成”的关键保障：索引库中的收据是先前某次
执行写下的证据；回放时新建一个干净内核，只信任信封（签名输入），重新计算
状态根 / status / gas / 写集并与存证比对。任何字段不一致都抛
:class:`ReplayMismatch`。

命令行::

    python -m teachchain.replay --db teachchain.db
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any

from .errors import ReplayMismatch, Rejected
from .kernel import ChainState, Kernel
from .storage import IndexStore
from .version import ENGINE_VERSION

# 必须逐字段一致的收据字段
COMPARE_FIELDS = (
    "status", "reverted", "halt_code", "output",
    "gas_limit", "intrinsic_gas", "gas_exec_used",
    "gas_refund", "gas_charged", "writes",
    "deployed_address", "pre_state_root", "post_state_root",
    "engine_version",
)


@dataclass
class ReplayReport:
    tx_total: int = 0
    matched: int = 0
    rejected_replayed: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    final_state_root: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "tx_total": self.tx_total,
            "matched": self.matched,
            "rejected_replayed": self.rejected_replayed,
            "mismatches": self.mismatches,
            "final_state_root": self.final_state_root,
            "engine_version": ENGINE_VERSION,
            "ok": not self.mismatches,
        }


def replay_store(store: IndexStore, *, stop_on_first: bool = True,
                 diag=None) -> ReplayReport:
    state = ChainState()
    # 先按有序流水重建初始余额（合成注资），再重放全部交易信封。
    for address, amount, reason in store.credits():
        state.balances[address] = state.balances.get(address, 0) + amount
    kernel = Kernel(state, diag=diag)
    report = ReplayReport()

    for height, envelope in store.list_envelopes():
        report.tx_total += 1
        stored = store.get_receipt(height)
        if stored is None:  # pragma: no cover - 写路径保证同事务
            raise ReplayMismatch(height, envelope.get("tx", {}).get("from", "?"),
                                 "receipt", "missing", "present")
        try:
            receipt = kernel.apply_tx(envelope)
        except Rejected as rej:
            # 已入块交易重放时绝不允许再被拒绝
            raise ReplayMismatch(height, stored["tx_hash"], "admission",
                                 "accepted", f"rejected:{rej.code}") from rej
        computed = receipt.data
        for fld in COMPARE_FIELDS:
            if computed.get(fld) != stored.get(fld):
                mismatch = {
                    "height": height,
                    "tx_hash": stored["tx_hash"],
                    "field": fld,
                    "stored": stored.get(fld),
                    "computed": computed.get(fld),
                }
                report.mismatches.append(mismatch)
                if stop_on_first:
                    raise ReplayMismatch(
                        height, stored["tx_hash"], fld,
                        stored.get(fld), computed.get(fld),
                    )
        if computed["result_digest"] != stored["result_digest"]:
            mismatch = {
                "height": height, "tx_hash": stored["tx_hash"],
                "field": "result_digest",
                "stored": stored["result_digest"],
                "computed": computed["result_digest"],
            }
            report.mismatches.append(mismatch)
            if stop_on_first:
                raise ReplayMismatch(height, stored["tx_hash"], "result_digest",
                                     stored["result_digest"],
                                     computed["result_digest"])
        report.matched += 1

    report.final_state_root = state.state_root()
    snapshot = store.snapshot_roots()
    if snapshot:
        last = max(snapshot)
        if snapshot[last] != report.final_state_root:
            mismatch = {
                "height": last, "tx_hash": "-",
                "field": "final_state_root",
                "stored": snapshot[last],
                "computed": report.final_state_root,
            }
            report.mismatches.append(mismatch)
            if stop_on_first:
                raise ReplayMismatch(last, "-", "final_state_root",
                                     snapshot[last], report.final_state_root)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="离线回放并核对 teachchain 数据库")
    ap.add_argument("--db", default="teachchain.db")
    ap.add_argument("--all", action="store_true",
                    help="列出全部不一致而非在第一处停止")
    args = ap.parse_args(argv)
    store = IndexStore(args.db)
    try:
        report = replay_store(store, stop_on_first=not args.all)
    finally:
        store.close()
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    return 0 if report.summary()["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
