"""HTTP 层 Pydantic schema。只做形状/类型校验，语义校验在服务层。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CreateTableRequest(BaseModel):
    table_id: str = Field(min_length=1)
    columns: dict[str, str]
    key_columns: list[str] = Field(alias="key", min_length=1)

    model_config = {"populate_by_name": True}

    @field_validator("columns")
    @classmethod
    def _non_empty(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("columns 不能为空")
        return v


class LoadRequest(BaseModel):
    file_id: str = Field(min_length=1)
    source: dict[str, Any]

    @field_validator("source")
    @classmethod
    def _source_shape(cls, v: dict[str, Any]) -> dict[str, Any]:
        if v.get("kind") not in ("inline", "inbox"):
            raise ValueError("source.kind 必须是 inline 或 inbox")
        return v


class DeleteRequestItem(BaseModel):
    delete_id: str = Field(min_length=1)
    kind: Literal["position", "equality"]
    file_id: str | None = None
    row_number: int | None = Field(default=None, ge=0)
    key: dict[str, Any] | None = None


class DeleteBatchRequest(BaseModel):
    deletes: list[DeleteRequestItem] = Field(min_length=1)


class RewriteRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    new_file_id: str = Field(min_length=1)


class FilterSpec(BaseModel):
    column: str = Field(min_length=1)
    op: Literal["eq", "neq", "gt", "gte", "lt", "lte", "is_null", "not_null"]
    value: Any = None


class QueryRequest(BaseModel):
    filters: list[FilterSpec] | None = None
    columns: list[str] | None = None
