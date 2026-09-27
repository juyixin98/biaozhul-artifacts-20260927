"""API 数据模型(Pydantic),与内部算法表示解耦。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MergeRequest(BaseModel):
    base: str
    local: str
    remote: str
    document_id: str | None = None


class ConflictModel(BaseModel):
    index: int
    kind: str
    base_range: list[int]
    local_range: list[int]
    remote_range: list[int]
    local_lines: list[str]
    base_lines: list[str]
    remote_lines: list[str]


class MergeResponse(BaseModel):
    merge_id: str
    request_id: str
    status: str
    text: str
    conflicts: list[ConflictModel]
    notes: list[str]


class ResolveRequest(BaseModel):
    # JSON 对象的键是字符串,服务侧转成 int;值为 "local" | "base" | "remote"
    choices: dict[str, str] = Field(default_factory=dict)


class ResolveResponse(BaseModel):
    merge_id: str
    request_id: str
    resolved_text: str


class VersionRequest(BaseModel):
    role: str
    content: str


class VersionResponse(BaseModel):
    version_id: str
    document_id: str
    role: str
    sha256: str
    line_ending: str
    ends_with_newline: bool
    line_count: int


class MergeByVersionsRequest(BaseModel):
    base_version: str
    local_version: str
    remote_version: str


class ErrorBody(BaseModel):
    category: str
    detail: str


class ErrorResponse(BaseModel):
    error: ErrorBody
