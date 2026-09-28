"""FastAPI 接口的 pydantic 请求模型（边界处反序列化与初步形状校验）。

深度语义校验（类型取值、状态冲突、序列号规则）在 services.planner 内完成，
保证 POST /validate 与 POST /commit 走同一条规划路径、错误完全一致。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ColumnModel(BaseModel):
    name: str
    type: str


class CreateTableModel(BaseModel):
    name: str
    columns: list[ColumnModel]
    primary_key: list[str] = Field(..., alias="primary_key")
    config: dict[str, int] | None = None

    model_config = {"populate_by_name": True}


class AppendOp(BaseModel):
    op: str = "append"
    ref: str  # 客户端对本次新增文件的临时引用名，提交响应中映射为服务端 file_id
    rows: list[dict[str, Any]]


class RewriteOp(BaseModel):
    op: str = "rewrite"
    ref: str
    rows: list[dict[str, Any]]
    drops: list[str]  # 被替换的旧文件 id


class PositionDeleteOp(BaseModel):
    op: str = "position_delete"
    ref: str | None = None
    target_file: str  # data file id（跨文件删除即指向其他现存数据文件）
    positions: list[int]  # 0 基行号，针对 target_file 的当前内容


class EqualityDeleteOp(BaseModel):
    op: str = "equality_delete"
    ref: str | None = None
    predicates: list[dict[str, Any]]
    # 每项形如 {"key": {"col": value, ...}}；缺列或键值为 null 按语义拒绝/不命中


class CommitModel(BaseModel):
    table_id: str
    parent_snapshot_id: str | None = None
    operations: list[dict[str, Any]]  # 多态操作，由 planner 按 'op' 分发校验


class FilterModel(BaseModel):
    """过滤 DSL：{col, op, value} 或 {and|or: [...]} 或 {col, op: 'is_null'|'not_null'}。

    过滤在删除应用之后执行；被删除行不会因过滤条件而“复活”，也不参与过滤。
    """

    filter: dict[str, Any] | None = None
    columns: list[str] | None = None
    snapshot_id: str | None = None


class ExplainModel(BaseModel):
    snapshot_id: str | None = None
    columns: list[str] | None = None
    filter: dict[str, Any] | None = None
    include_filtered: bool = True
    include_deleted: bool = True
    snapshot_seq: int | None = None  # 只读别名：也可用 seq 指定版本


class ValidateModel(BaseModel):
    table_id: str
    parent_snapshot_id: str | None = None
    operations: list[dict[str, Any]]
