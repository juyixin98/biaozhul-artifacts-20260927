"""Request/response schemas."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class SegmentRequest(BaseModel):
    text: str = Field(..., description="Original text to segment.")


class TokenOut(BaseModel):
    kind: str
    surface: str
    display: str
    cost: float
    norm_start: int
    norm_end: int
    orig_start: int
    orig_end: int
    is_unknown: bool


class PathOut(BaseModel):
    surfaces: list[str]
    cost: float
    token_count: int


class SegmentResponse(BaseModel):
    request_id: str
    version: str
    text_length: int
    normalized_text: str
    tokens: list[TokenOut]
    best: PathOut
    runner_up: Optional[PathOut]
    gap: Optional[float]
    gap_rounded: Optional[float]
    gap_status: str
    coverage: dict[str, Any]
    unknown_tokens: int
    removed_chars: int
    diagnostics: dict[str, Any]


class EntryIn(BaseModel):
    surface: str
    frequency: Optional[int] = None
    cost: Optional[float] = None


class PublishRequest(BaseModel):
    entries: list[EntryIn]
    note: Optional[str] = None


class VersionOut(BaseModel):
    version: str
    created_at: str
    entry_count: int
    total_frequency: int
    checksum: str
    note: Optional[str]
    is_current: bool
