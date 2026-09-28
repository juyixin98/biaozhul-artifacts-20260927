"""节点装配：把链状态内核与 SQLite 索引组合为一个本地服务对象。

封块过程串行化（进程内锁），保证索引与内存状态同步推进。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .kernel import ChainState, ProcessedTransaction, normalize_transaction
from .store import IndexStore


class NodeError(RuntimeError):
    pass


@dataclass
class SubmissionResult:
    block_number: int
    block_hash: str
    results: list[ProcessedTransaction]


class LocalNode:
    """本地单进程教学节点。"""

    def __init__(self, db_path: str | Path, chain: str = "teaching-chain-local"):
        self.chain = chain
        self.store = IndexStore(db_path)
        self.store.initialize(chain)
        self._lock = threading.RLock()
        self.state = ChainState(chain=chain)
        self._restore_from_store()

    def _restore_from_store(self) -> None:
        """冷启动：从索引库顺序重放，重建内存 KV 状态与链尖。"""
        with self._lock:
            storage: dict[int, int] = {}
            height = -1
            head_hash = self.store.head()[1]
            from .kernel import process_transaction

            for number, _h, _p, _c in self.store.iter_blocks():
                # 逐笔重放原文以重建 KV 状态（收据不含全量状态快照）
                txs = [
                    normalize_transaction(self.store.get_transaction(receipt.tx_hash))
                    for receipt in self.store.receipts_for_block(number)
                ]
                for idx, tx in enumerate(txs):
                    processed = process_transaction(
                        tx, storage, self.chain, number, idx, record_trace=False
                    )
                    storage = processed.storage_after
                height = number
            self.state.storage = storage
            self.state.height = height
            self.state.head_hash = head_hash

    # ------------------------------------------------------------------
    def submit(self, transactions: list[dict[str, Any]]) -> SubmissionResult:
        """校验整批交易、执行、封块并落索引。

        静态校验失败抛 ``TransactionError``（调用方映射为 4xx）；
        整批原子：任何一笔静态失败则不执行、不写块。
        """
        with self._lock:
            block, processed = self.state.apply_block(transactions)
            try:
                self.store.append_block(block)
            except Exception:
                # 索引写入失败：回退内存中刚封的块，保持两者一致
                self._rollback_memory_block(block)
                raise
            return SubmissionResult(block.number, block.hash(), processed)

    def _rollback_memory_block(self, block: Any) -> None:
        # 重新从索引重建（简单且确定，教学规模成本可忽略）
        self.state = ChainState(chain=self.chain)
        self._restore_from_store()

    def dry_run(self, transaction: dict[str, Any]) -> ProcessedTransaction:
        with self._lock:
            return self.state.dry_run(transaction)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "chain": self.chain,
                "height": self.state.height,
                "head_hash": self.state.head_hash,
                "state_root": self.state.state_root(),
            }

    def block(self, number: int) -> dict[str, Any] | None:
        return self.store.get_block_header(number)

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        return self.store.get_receipt_by_tx_hash(tx_hash)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "LocalNode":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
