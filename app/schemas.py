"""HTTP 层的 Pydantic 模型（请求/响应结构）。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class EntryIn(BaseModel):
    """单个写入词条。

    - ``id`` 由客户端指定（便于“词频更新”定位同一条），省略时服务端生成；
    - ``term`` 是展示原文，原样保存；规范化键由服务端按固定版本生成；
    - ``score`` 为词频/权重，必须是有限实数（NaN/无穷会被服务端拒绝）。
    """

    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    term: str = Field(min_length=1, max_length=4096)
    score: float


class BulkUpsertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[EntryIn] = Field(min_length=1, max_length=10_000)
    client_batch_id: Optional[str] = Field(default=None, max_length=128)
    note: str = Field(default="", max_length=512)


class DeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=256)


class CompletionOut(BaseModel):
    term: str          # 展示原文
    id: str
    score: float
    term_norm: str     # 规范化索引键（诊断可见，便于解释同键碰撞）


class CompletionResponse(BaseModel):
    query_prefix: str
    prefix_norm: str
    normalizer_version: str
    version: int
    k: int
    count: int
    results: list[CompletionOut]


class VersionInfo(BaseModel):
    version_id: int
    parent_id: Optional[int]
    kind: Literal["baseline", "commit", "snapshot", "restore"]
    normalizer_version: str
    entry_count: int
    note: str
    created_at: str


class BulkUpsertResponse(BaseModel):
    version: int
    parent_version: int
    client_batch_id: str
    inserted: int
    updated: int
    entry_count: int
    results: list[CompletionOut] = Field(default_factory=list)


class DeleteResponse(BaseModel):
    version: int
    id: str
    deleted: bool
    entry_count: int


class SnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=512)


class SnapshotResponse(BaseModel):
    version: int
    entry_count: int
    note: str
    created_at: str


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    note: str = Field(default="", max_length=512)


class RestoreResponse(BaseModel):
    version: int
    source_snapshot_version: int
    entry_count: int


class ErrorBody(BaseModel):
    error_code: str
    message: str
    request_id: str
    version: Optional[int] = None
    normalizer_version: Optional[str] = None
    field: Optional[str] = None
