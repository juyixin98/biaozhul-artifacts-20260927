"""Pydantic 请求/响应模式（所有二进制字段以 hex 字符串传输）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class UpdateItem(BaseModel):
    key: str = Field(..., description="定长键的 hex（默认 32 字节）")
    value: str | None = Field(
        ..., description="值的 hex；null=删除该键；空串 \"\"=存在且值为空字节串"
    )


class BatchUpdateRequest(BaseModel):
    updates: list[UpdateItem]
    idempotency_key: str | None = Field(
        None, description="可选幂等键：同键同载荷返回同版本，同键异载荷 409"
    )


class RootResponse(BaseModel):
    version: int
    root: str
    parent_root: str | None = None
    batch_id: str | None = None
    changed: int | None = None
    idempotent_replay: bool | None = None


class ValueResponse(BaseModel):
    key: str
    exists: bool
    value: str | None  # 不存在 -> null；存在空值 -> ""
    version: int


class ProofResponse(BaseModel):
    proof: dict
    exists: bool
    value: str | None
    version: int


class VerifyRequest(BaseModel):
    proof: dict
    expect_membership: bool | None = None
    expect_value: str | None = None


class VerifyResponse(BaseModel):
    decision: str
    reason: str
    message: str
    key_fingerprint: str | None = None
    claimed_root: str | None = None
    recomputed_root: str | None = None
    detail: dict = {}


class ErrorResponse(BaseModel):
    error: str
    reason: str
    message: str
    request_id: str
    detail: dict = {}


class VersionResponse(BaseModel):
    version: int
    root: str
    parent_root: str | None
    batch_id: str
    changed: int
    created_at: float
