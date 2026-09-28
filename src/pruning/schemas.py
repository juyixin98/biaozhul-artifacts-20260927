"""HTTP API 的 Pydantic 模式（仅 Web 边界使用，核心保持框架无关）。"""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class PredicateIn(BaseModel):
    column: str
    kind: str = Field(description="range|eq|in|is_null|not_null")
    value: Optional[Any] = None
    values: Optional[list[Any]] = None
    lower: Optional[Any] = None
    upper: Optional[Any] = None
    lower_inclusive: bool = True
    upper_inclusive: bool = False


class RegisterIn(BaseModel):
    table: str
    partition_column: str = "event_ts"
    truncated_string_columns: list[str] = []
    missing_stat_columns: list[str] = []
    truncate_prefix_len: int = 4


class PlanIn(BaseModel):
    table: str
    request_id: Optional[str] = None
    predicates: list[PredicateIn]


class ValidateIn(PlanIn):
    pass
