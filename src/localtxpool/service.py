"""服务容器：把存储、时钟、内核与配置装在一起，供 API 与 CLI 复用。

提交入口也在这里：HTTP 层只做参数解析，真正的“解码验签 -> 事务入库 ->
分类”流水线集中于 :meth:`Service.submit_raw`，因此离线回放与在线接口
执行的是**同一段**核心逻辑。
"""

from __future__ import annotations

import uuid

from .clock import Clock, VirtualClock
from .config import Config
from .core.chain import Chain
from .core.mempool import Mempool
from .encoding import decode_signed, to_checksum_address
from .storage.repository import Repository


class Service:
    def __init__(self, config: Config, clock: Clock | None = None,
                 repo: Repository | None = None):
        self.config = config
        self.clock = clock or Clock()
        self.repo = repo or Repository(config.storage.path)
        self.pool = Mempool(self.repo, config.pool, self.clock)
        self.chain = Chain(self.repo, self.pool, config.chain, self.clock)
        self._ensure_genesis()

    def _ensure_genesis(self) -> None:
        if self.repo.get_meta("genesis") is None:
            self.repo.set_meta("genesis", "1")
            self.repo.set_meta("chain_id", str(self.config.chain.chain_id))

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex

    def submit_raw(self, raw: bytes, *, request_id: str) -> dict:
        """解码验签并提交一条原始 RLP 交易（单个数据库事务）。"""
        tx = decode_signed(raw, expected_chain_id=self.config.chain.chain_id)
        with self.repo.transaction():
            return self.pool.accept(tx, request_id=request_id)

    def close(self) -> None:
        self.repo.close()

    # ---- 测试/回放便捷构造 ---- #
    @classmethod
    def in_memory(cls, config: Config, clock: VirtualClock | Clock | None = None) -> "Service":
        return cls(config, clock=clock, repo=Repository(":memory:"))

    def sender_address(self, raw_sender: bytes) -> str:
        return to_checksum_address(raw_sender)
