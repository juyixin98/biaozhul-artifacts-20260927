"""链上数据模型（pydantic）。

用于 API 与 JSON 夹具的形状校验；内核与存储以普通 dict 工作，
模型只负责边界处的解析与序列化，避免把框架异常泄漏进领域规则。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import crypto


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_pubkey: str
    recipient: str
    amount: int = Field(ge=0)
    nonce: int = Field(ge=0, default=0)
    memo: str = ""
    tx_id: str
    signature: str

    def to_dict(self) -> dict:
        return self.model_dump()


class BlockHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = crypto.BLOCK_VERSION
    prev_hash: str
    height: int = Field(ge=0)
    merkle_root: str
    weight: int = Field(ge=1)
    proposer: str = ""
    signature: str = ""
    block_hash: str

    def to_dict(self) -> dict:
        return self.model_dump()


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header: BlockHeader
    txs: list[Transaction]

    def to_dict(self) -> dict:
        return {"header": self.header.to_dict(), "txs": [t.to_dict() for t in self.txs]}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Block":
        return Block.model_validate(data)

    @staticmethod
    def from_json_bytes(raw: bytes) -> "Block":
        import json

        return Block.from_dict(json.loads(raw.decode("utf-8")))


# ---------------------------------------------------------------- 内核结果

class RollbackRange(dict):
    """回滚区间：[from_height, to_height]（含端点），按高度降序的区块哈希。"""


class IngestOutcome(BaseModel):
    """ingest 的完整结论；status 取值见 api 映射。"""

    status: str
    block_hash: str
    height: int
    parent_hash: str
    canonical_changed: bool = False
    tip_hash: str | None = None
    tip_height: int | None = None
    tip_weight: int | None = None
    disconnected: list[str] = Field(default_factory=list)
    rollback_from_height: int | None = None
    rollback_to_height: int | None = None
    connected: list[str] = Field(default_factory=list)
    pending: bool = False
    pending_count: int = 0
    reason: str | None = None
