"""HTTP 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class IssueRequest(BaseModel):
    secret_b64: str = Field(..., description="base64 编码的待分享秘密（本地合成夹具）")
    threshold: int = Field(..., ge=2, le=255)
    share_count: int = Field(..., ge=2, le=255)
    labels: dict[str, str] | None = Field(
        None, description="可选参与者标签，键为 1..share_count"
    )


class RecoverRequest(BaseModel):
    shares: list[dict] = Field(..., description="份额信封对象数组（原样回传）")


class RecoverResponse(BaseModel):
    outcome: str
    category: str | None = None
    request_id: str
    set_id: str | None = None
    reason: str
    secret_b64: str | None = None
    secret_fp: str | None = None
    diagnostics: dict
    consistent_subsets: list[list[str]] = []
    always_good_x: list[int] = []
    enum_truncated: bool = False


class IssueResponse(BaseModel):
    set_id: str
    threshold: int
    share_count: int
    field: dict
    commitment: str
    secret_fp: str
    shares: list[dict]
    labels: dict[str, str]
