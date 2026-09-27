"""HTTP 请求/响应模式。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(..., description="布尔查询文本，如 cat AND NOT dog")
    version: Optional[int] = Field(None, description="版本号；缺省为最新版本")
    operand_order: str = Field(
        "left_to_right",
        description="AND/OR 子节点执行顺序：left_to_right | right_to_left",
    )
    unknown_terms_empty: bool = Field(
        False, description="为 true 时未知词项按空 posting 处理（记为不确定结论）"
    )


class CommitAdd(BaseModel):
    doc_id: int
    terms: List[str] = Field(default_factory=list)


class CommitRequest(BaseModel):
    parent_version: Optional[int] = Field(None, description="父版本；缺省为最新版本")
    adds: List[CommitAdd] = Field(default_factory=list)
    deletes: List[int] = Field(default_factory=list)
    message: str = ""


class ErrorBody(BaseModel):
    ok: bool = False
    error: Dict[str, Any]
    request_id: str
