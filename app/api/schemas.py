"""Pydantic request/response schemas for the HTTP boundary."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class CreateVersionIn(BaseModel):
    name: str = ""
    encoding: str = "utf-8"
    case_mode: str = "sensitive"
    patterns: List[str] = Field(
        ...,
        description="Patterns as canonical base64 strings; binary safe.",
    )


class VersionOut(BaseModel):
    version_id: str
    name: str
    encoding: str
    case_mode: str
    pattern_count: int
    node_count: int
    created_at: str


class OpenScanIn(BaseModel):
    version_id: str


class ScanStatusOut(BaseModel):
    scan_id: str
    version_id: str
    state: str
    state_node: int
    bytes_consumed: int
    epoch: int
    pattern_count: int
    node_count: int
    total_hits_in_epoch: int


class FeedChunkIn(BaseModel):
    chunk: str = Field(..., description="Canonical base64 of the raw bytes.")
    version_id: Optional[str] = Field(
        default=None,
        description="Optional guard; must equal the scan's pinned version.",
    )


class FeedChunkOut(BaseModel):
    scan_id: str
    version_id: str
    bytes_consumed: int
    state_node: int
    epoch: int
    new_hits: int
    first_seq: Optional[int]
    total_hits_in_epoch: int


class ResetIn(BaseModel):
    version_id: str


class HitOut(BaseModel):
    seq: int
    start: int
    end: int
    pattern_id: int
    pattern_length: int
    pattern_b64: str


class HitPageOut(BaseModel):
    scan_id: str
    version_id: str
    epoch: int
    limit: int
    total_in_epoch: int
    returned: int
    next_cursor: Optional[str]
    hits: List[HitOut]


class DiagnosticEventOut(BaseModel):
    id: int
    request_id: Optional[str]
    kind: str
    method: Optional[str]
    path: Optional[str]
    decision: Optional[str]
    code: Optional[str]
    summary: str
    state_json: Optional[str]
    created_at: str
