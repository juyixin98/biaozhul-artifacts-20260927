"""离线回放：从索引库读出全部交易，在空状态上重新执行并与存证收据逐笔比对。

核验口径：

* **ACCEPT（接受）**：存证收据可由当前程序版本从原始交易 + 前序状态
  确定性地重新生成（收据摘要逐字节一致），且区块父子哈希链接完整；
* **REJECT（拒绝）**：重新执行得到不同结果（状态根 / 费用 / 失败类别
  任一不一致），说明存证被篡改或实现语义已变；
* **UNDETERMINED（无法判定）**：存证来自不同 program_version，当前
  节点不具备复现它的语义承诺，只报告差异、不做接受结论。

回放不写任何数据；报告可在不同进程 / 机器上重复生成（结果含报告自身
的规范摘要，供跨进程比对）。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import encoding
from .config import PROGRAM_VERSION
from .kernel import GENESIS_PARENT, normalize_transaction, process_transaction, state_root
from .store import IndexStore

ACCEPT = "ACCEPT"
REJECT = "REJECT"
UNDETERMINED = "UNDETERMINED"


@dataclass
class TxCheck:
    block_number: int
    tx_index: int
    tx_hash: str
    verdict: str
    reasons: list[str] = field(default_factory=list)
    stored_status: int | None = None
    replayed_status: int | None = None
    stored_gas_used: int | None = None
    replayed_gas_used: int | None = None
    stored_error: str | None = None
    replayed_error: str | None = None
    state_rolled_back: bool | None = None  # 失败交易的状态回滚是否得到核验

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_number": self.block_number,
            "tx_index": self.tx_index,
            "tx_hash": self.tx_hash,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "stored_status": self.stored_status,
            "replayed_status": self.replayed_status,
            "stored_gas_used": self.stored_gas_used,
            "replayed_gas_used": self.replayed_gas_used,
            "stored_error": self.stored_error,
            "replayed_error": self.replayed_error,
            "state_rolled_back": self.state_rolled_back,
        }


@dataclass
class ReplayReport:
    chain: str
    program_version: str
    block_count: int
    tx_count: int
    accepted: int = 0
    rejected: int = 0
    undetermined: int = 0
    checks: list[TxCheck] = field(default_factory=list)
    block_problems: list[str] = field(default_factory=list)
    final_state_root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "program_version": self.program_version,
            "block_count": self.block_count,
            "tx_count": self.tx_count,
            "summary": {
                ACCEPT: self.accepted,
                REJECT: self.rejected,
                UNDETERMINED: self.undetermined,
            },
            "final_state_root": self.final_state_root,
            "block_problems": self.block_problems,
            "checks": [c.to_dict() for c in self.checks],
        }

    def digest(self) -> str:
        """报告规范摘要：跨进程两次回放该值必须相同。"""
        body = {
            "chain": self.chain,
            "program_version": self.program_version,
            "final_state_root": self.final_state_root,
            "block_problems": self.block_problems,
            "checks": [c.to_dict() for c in self.checks],
        }
        return encoding.hexhash(body)


def replay(store: IndexStore) -> ReplayReport:
    chain = store.chain
    report = ReplayReport(
        chain=chain,
        program_version=PROGRAM_VERSION,
        block_count=0,
        tx_count=0,
    )

    # 区块结构自检
    report.block_problems = store.integrity_check()

    storage: dict[int, int] = {}
    expected_parent = GENESIS_PARENT
    for number, block_hash, parent_hash, tx_count in store.iter_blocks():
        report.block_count += 1
        if parent_hash != expected_parent:
            report.block_problems.append(
                f"区块 {number} 父链接断裂：记录 {parent_hash[:16]}…，"
                f"期望 {expected_parent[:16]}…"
            )
        header = store.get_block_header(number)
        if header is not None and encoding.hexhash(header) != block_hash:
            report.block_problems.append(f"区块 {number} 头重哈希不匹配")

        stored_receipts = store.receipts_for_block(number)
        # 重新规整并按序执行
        block_txs: list[dict[str, Any]] = []
        # transactions 表没有按块的便捷迭代器，按 receipt 的 tx_hash 取回原文
        for receipt in stored_receipts:
            tx = store.get_transaction(receipt.tx_hash)
            if tx is None:
                report.block_problems.append(
                    f"区块 {number} 缺少交易原文 {receipt.tx_hash[:16]}…"
                )
                continue
            block_txs.append(normalize_transaction(tx))

        for idx, tx in enumerate(block_txs):
            report.tx_count += 1
            stored = stored_receipts[idx]
            check = TxCheck(
                block_number=number,
                tx_index=idx,
                tx_hash=stored.tx_hash,
                verdict=ACCEPT,
                stored_status=stored.status,
                stored_gas_used=stored.gas_used,
                stored_error=stored.error_category,
            )
            storage_before = dict(storage)
            processed = process_transaction(
                tx, storage, chain, number, idx,
                record_trace=False,
            )
            replayed = processed.receipt
            check.replayed_status = replayed.status
            check.replayed_gas_used = replayed.gas_used
            check.replayed_error = replayed.error_category

            if stored.program_version != PROGRAM_VERSION:
                check.verdict = UNDETERMINED
                check.reasons.append(
                    f"存证程序版本 {stored.program_version!r} 与当前 "
                    f"{PROGRAM_VERSION!r} 不同，语义不可保证一致"
                )
            else:
                if stored.digest() != replayed.digest():
                    check.verdict = REJECT
                    if stored.status != replayed.status:
                        check.reasons.append(
                            f"成败不一致：存证 status={stored.status}，"
                            f"回放 status={replayed.status}"
                        )
                    if stored.gas_used != replayed.gas_used:
                        check.reasons.append(
                            f"费用消耗不一致：存证 gas_used={stored.gas_used}，"
                            f"回放 gas_used={replayed.gas_used}"
                        )
                    if stored.error_category != replayed.error_category:
                        check.reasons.append(
                            f"失败类别不一致：存证 {stored.error_category}，"
                            f"回放 {replayed.error_category}"
                        )
                    if stored.state_root != replayed.state_root:
                        check.reasons.append(
                            "状态根不一致：存证 "
                            f"{stored.state_root[:16]}…，回放 "
                            f"{replayed.state_root[:16]}…"
                        )
                    if stored.return_value != replayed.return_value:
                        check.reasons.append(
                            f"返回值不一致：{stored.return_value} vs {replayed.return_value}"
                        )
                    if not check.reasons:
                        check.reasons.append("收据摘要不一致（以上关键字段相同，请比对完整收据）")
                else:
                    check.verdict = ACCEPT

            # 回滚范围核验（无论版本是否一致都如实计算；仅失败交易）
            if replayed.status == 0:
                check.state_rolled_back = processed.storage_after == storage_before

            storage = processed.storage_after

            if check.verdict == ACCEPT:
                report.accepted += 1
            elif check.verdict == REJECT:
                report.rejected += 1
            else:
                report.undetermined += 1
            report.checks.append(check)

        # 逐块状态根比对
        if header is not None and header.get("state_root") != state_root(storage):
            report.block_problems.append(
                f"区块 {number} 状态根不一致：存证 {header.get('state_root','')[:16]}…，"
                f"回放 {state_root(storage)[:16]}…"
            )
        expected_parent = block_hash

    report.final_state_root = state_root(storage)
    return report


def report_to_json(report: ReplayReport) -> str:
    data = report.to_dict()
    data["report_digest"] = report.digest()
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teaching-chain-replay",
        description="对索引库执行确定性离线回放并输出核验报告",
    )
    parser.add_argument("--db", default=None, help="SQLite 索引路径（默认取配置目录）")
    parser.add_argument("--out", default=None, help="报告写入路径（默认打印到 stdout）")
    parser.add_argument(
        "--fail-on-reject", action="store_true",
        help="存在 REJECT 时以退出码 2 结束（CI 用）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    from .config import get_settings

    settings = get_settings(args.db)
    db_path = settings.resolved_db_path()
    if not db_path.exists():
        print(report_to_json(ReplayReport(
            chain="", program_version=PROGRAM_VERSION, block_count=0, tx_count=0
        )))
        return 0
    with IndexStore(db_path) as store:
        report = replay(store)
    text = report_to_json(report)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"报告已写入 {args.out}（digest={report.digest()}）", file=sys.stderr)
    else:
        print(text)
    if args.fail_on_reject and report.rejected:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
