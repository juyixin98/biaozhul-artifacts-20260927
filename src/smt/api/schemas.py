"""Request/response Pydantic schemas."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class UpdateItem(BaseModel):
    key: str = Field(..., description="32-byte key as 64 hex chars")
    value: Optional[str] = Field(
        None, description="UTF-8 string to store; null deletes the key"
    )


class BatchUpdateRequest(BaseModel):
    updates: List[UpdateItem] = Field(..., min_length=1, max_length=1024)


class EffectOut(BaseModel):
    seq: int
    kind: str
    key: str
    value_byte_length: int
    prev_root: str
    new_root: str


class BatchUpdateResponse(BaseModel):
    request_id: str
    revision: int
    root: str
    effects: List[EffectOut]


class ProofResponse(BaseModel):
    request_id: str
    root: str
    revision: Optional[int] = None
    proof: dict


class VerifyRequest(BaseModel):
    proof: dict


class VerifyResponse(BaseModel):
    request_id: str
    valid: bool
    verdict: str
    reason: str


class RootOut(BaseModel):
    request_id: str
    revision: int
    root: str


class RevisionOut(BaseModel):
    seq: int
    root: str
    created_at: str
    note: str


class RevisionListResponse(BaseModel):
    request_id: str
    revisions: List[RevisionOut]


class HealthResponse(BaseModel):
    status: str
    spec: str
    revision: int
    root: str
