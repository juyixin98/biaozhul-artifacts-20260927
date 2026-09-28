"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class WholeRedactRequest(BaseModel):
    text: str = Field(..., description="待脱敏日志原文（合成数据）")
    profile: str | None = Field(None, description="规则档名；缺省用默认档")


class MappingOut(BaseModel):
    index: int
    rule_id: str
    source: str
    key: str | None = None
    original_span: list[int]
    output_span: list[int]
    replacement: str
    original_sha256: str


class UncertaintyOut(BaseModel):
    code: str
    start: int
    end: int
    detail: str


class RejectionOut(BaseModel):
    winner: str
    loser: str
    winner_span: list[int]
    loser_span: list[int]
    reason: str


class RedactionResponse(BaseModel):
    request_id: str
    status: Literal["ok", "error"]
    redacted_text: str
    profile: str
    profile_version: str
    engine_version: str
    original_length: int
    output_length: int
    mappings: list[MappingOut]
    uncertainties: list[UncertaintyOut]
    rejected: list[RejectionOut]
    residual_findings: list[str]
    error_code: str | None = None
    error_message: str | None = None
    steps: list[dict[str, Any]] = []


class StreamOpenRequest(BaseModel):
    profile: str | None = None


class StreamOpenResponse(BaseModel):
    request_id: str
    profile: str
    profile_version: str


class StreamChunkRequest(BaseModel):
    request_id: str
    chunk: str
    final: bool = False


class StreamChunkResponse(BaseModel):
    request_id: str
    emitted_text: str
    emitted_original_span: list[int]
    held_chars: int
    finalized: bool
    result: RedactionResponse | None = None


class ErrorBody(BaseModel):
    error_code: str
    message: str
    request_id: str | None = None
