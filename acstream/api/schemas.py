"""请求/响应 Pydantic 模型。

注意：空字符串 data、空模式列表不在 Pydantic 层拦截——它们要带着专门的
错误码（EMPTY_PATTERN / EMPTY_PATTERN_SET）由算法层拒绝，而不是混入通用
VALIDATION_ERROR。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Encoding = Literal["utf-8", "base64", "hex", "raw"]


class PatternIn(BaseModel):
    id: str = Field(..., min_length=1, description="调用方给定的模式标识")
    data: str = Field(..., description="按 encoding 编码后的模式字节串")


class CreateVersionIn(BaseModel):
    encoding: Encoding = "utf-8"
    patterns: list[PatternIn]


class MatchIn(BaseModel):
    encoding: Encoding = "utf-8"
    data: str
    patterns: list[PatternIn]


class OpenSessionIn(BaseModel):
    version_id: str = Field(..., min_length=1)


class FeedIn(BaseModel):
    encoding: Encoding = "utf-8"
    data: str = ""
    expected_offset: int | None = Field(
        default=None, ge=0, description="乐观并发：客户端认为下一块应处的字节偏移"
    )
    expected_fingerprint: str | None = Field(
        default=None, description="客户端认为当前会话绑定的自动机指纹"
    )
    finish: bool = False


class SwitchVersionIn(BaseModel):
    version_id: str = Field(..., min_length=1)


class HitOut(BaseModel):
    pattern_id: str
    start: int
    end: int
    feed_seq: int | None = None
