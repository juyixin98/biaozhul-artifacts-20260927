"""离线回放（模块四）。

不依赖网络/服务：给定一个 bundle（创世纪 UTXO + 有序交易列表），
在*全新内存 SQLite* 上用同一 ChainKernel 顺序重放，输出每笔交易的
确定性判定（接受/失败分类）、最终状态根与 UTXO 摘要。

用途：
- 对已记录的问题交易做可复核重放（runs.jsonl 里能取到 run_id 与中间状态）；
- CI 中对 fixtures/bundles/*.json 做回归；
- 回答“换个顺序/重复签名/双花时会怎样”——只算不转账。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..chain import ChainKernel
from ..config import Settings, load_settings
from ..errors import VerificationFailure
from ..storage import SQLiteStore


@dataclass
class ReplayResult:
    bundle_id: str
    network: str
    results: list[dict] = field(default_factory=list)
    final_state_root: str = ""
    final_utxos: list[dict] = field(default_factory=list)
    accepted_count: int = 0
    rejected_count: int = 0

    def to_dict(self) -> dict:
        return {
            "bundle_id": self.bundle_id,
            "network": self.network,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "final_state_root": self.final_state_root,
            "final_utxos": self.final_utxos,
            "results": self.results,
        }


def replay_bundle(bundle: dict, runs_dir: str | None = None) -> ReplayResult:
    settings = load_settings(sqlite_path=":memory:", runs_dir=runs_dir or "runs/replay")
    store = SQLiteStore(":memory:")
    kernel = ChainKernel(settings, store=store)

    genesis = bundle["genesis"]
    boot = kernel.bootstrap_from_dict(genesis)

    out = ReplayResult(
        bundle_id=bundle.get("bundle_id", "unknown"),
        network=settings.chain.network,
    )
    out.results.append({"seq": 0, "phase": "bootstrap", **boot})

    for seq, tx in enumerate(bundle.get("transactions", []), start=1):
        rep = kernel.verify_transaction_dict(tx, persist=True, kind="replay")
        row = {
            "seq": seq,
            "accepted": rep.accepted,
            "run_id": rep.run_id,
            "txid": rep.txid,
            "reason": rep.reason,
        }
        if rep.failure:
            row["failure"] = rep.failure
        else:
            row["steps_used"] = rep.steps_used
            row["message32"] = rep.message32
        out.results.append(row)
        if rep.accepted:
            out.accepted_count += 1
        else:
            out.rejected_count += 1

    out.final_state_root = store.state_root()
    out.final_utxos = [
        {
            "txid": u.txid,
            "index": u.idx,
            "value": u.value,
            "domain": u.domain,
            "pubkey_script": u.pubkey_script,
        }
        for u in store.list_utxos()
    ]
    return out


def replay_bundle_file(path: str | Path, runs_dir: str | None = None) -> ReplayResult:
    bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    return replay_bundle(bundle, runs_dir=runs_dir)
