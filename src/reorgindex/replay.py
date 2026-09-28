"""离线回放与全量重建核对（不经过服务）。

两个能力：
1. 从 JSONL 夹具按文件顺序回放区块到一个内核/库（离线灌库）。
2. 全量重建（rebuild）：丢弃派生表，用与在线同一份规则按"接收顺序"重放
   库中全部区块，得到权威链与派生余额，再与当前派生索引逐项比对。

参考答案不是用被测内核重新生成：``reference_oracle`` 是一个独立实现的
朴素预言机（见 tests 与 reference.py 的最小副本），它自行按规则选链、
去重、累计，用于交叉验证 rebuild 与在线索引一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import IndexError as DomainError
from .kernel import ChainKernel
from .storage import Storage


@dataclass
class ReplayReport:
    ingested: int = 0
    pending_final: int = 0
    rejected: list[dict] = field(default_factory=list)
    switched: int = 0
    reorgs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ingested": self.ingested,
            "pending_final": self.pending_final,
            "rejected": self.rejected,
            "switched": self.switched,
            "reorgs": self.reorgs,
        }


def replay_jsonl(kernel: ChainKernel, path: str | Path, request_id: str | None = "offline-replay") -> ReplayReport:
    """逐行读取 JSON 区块并 ingest；非法/被拒行记录类别后继续后续区块。"""

    report = ReplayReport()
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                block = json.loads(raw)
            except json.JSONDecodeError as exc:
                report.rejected.append({"line": lineno, "category": "decode_error", "detail": str(exc)})
                continue
            try:
                outcome = kernel.ingest(block, request_id=request_id)
                report.ingested += 1
                if outcome.canonical_changed and outcome.status == "switched":
                    report.switched += 1
            except DomainError as exc:
                report.rejected.append({"line": lineno, "category": exc.category, "detail": exc.message})
    report.pending_final = kernel.storage.pending_count()
    report.reorgs = kernel.storage.recent_reorgs(limit=100)
    return report


def rebuild_in_memory(kernel: ChainKernel) -> dict:
    """用库里存储的原始区块在内存中全量重建权威链与派生结果。

    重建严格按 received_seq（首次接收顺序）重放，使用同一套内核规则；
    这验证"在线增量维护"与"从零重放"得到同一条完整链版本。
    """

    rows = kernel.storage._conn.execute(
        "SELECT raw, received_seq FROM blocks ORDER BY received_seq, rowid"
    ).fetchall()
    scratch = Storage(":memory:")
    try:
        rebuilder = ChainKernel(scratch, kernel.settings)
        for row in rows:
            rebuilder.ingest(json.loads(row["raw"]), request_id="rebuild")
        return {
            "canonical_chain": rebuilder.canonical_chain(),
            "tip_hash": rebuilder.tip_hash(),
            "balances": rebuilder.all_balances(),
            "contribution_count": scratch.contribution_count(),
            "contributing_blocks": sorted(rebuilder.storage.contributing_block_hashes()),
        }
    finally:
        scratch.close()


def verify_against_rebuild(kernel: ChainKernel) -> dict:
    """把当前在线派生索引与全量重建结果逐项核对。"""

    rebuilt = rebuild_in_memory(kernel)
    current = {
        "canonical_chain": kernel.canonical_chain(),
        "tip_hash": kernel.tip_hash(),
        "balances": kernel.all_balances(),
        "contribution_count": kernel.storage.contribution_count(),
        "contributing_blocks": sorted(kernel.storage.contributing_block_hashes()),
    }
    mismatches: list[str] = []
    for key in ("canonical_chain", "tip_hash", "balances", "contribution_count", "contributing_blocks"):
        if current[key] != rebuilt[key]:
            mismatches.append(key)
    return {"ok": not mismatches, "mismatches": mismatches,
            "current": current, "rebuilt": rebuilt}
