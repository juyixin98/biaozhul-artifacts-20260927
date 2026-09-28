"""请求/响应模型。显式建模（无固定返回值），422 由 pydantic 先做形状校验。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FieldIn(BaseModel):
    name: str
    type: str
    nullable: bool = True


class CreateTableIn(BaseModel):
    name: str
    primary_key: list[str] = Field(min_length=1)
    fields: list[FieldIn] = Field(min_length=1)


class CommitSnapshotIn(BaseModel):
    rows: list[dict[str, Any]]


class CreateBranchIn(BaseModel):
    head_snapshot_id: str


class StartMergeIn(BaseModel):
    table: str
    ours_branch: str = "develop"
    theirs_branch: str = "main"
    base_snapshot_id: str | None = None


class ResolveConflictIn(BaseModel):
    # 主键值，按主键列顺序，例如 [3]；也接受 {列名: 值}
    key: list[Any] | dict[str, Any]
    kind: str  # ours | theirs | value | delete | keep
    custom_row: dict[str, Any] | None = None
    # 必须回传开启合并时的三方快照，服务端据此校验解决方案绑定
    bound_snapshots: tuple[str, str, str] | None = None


class CommitMergeIn(BaseModel):
    message: str | None = None
    target_branch: str | None = None


class CommitRowsIn(BaseModel):
    rows: list[dict[str, Any]]
