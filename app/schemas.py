"""Pydantic request/response models for the redaction API."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RedactRequest(BaseModel):
    text: str = Field(..., description="Log fragment to redact (synthetic data only)")
    profile: str = Field(default="standard")


class UncertainOut(BaseModel):
    rule_id: str
    label: str
    start: int
    end: int
    length: int
    reason: Optional[str] = None
    original_sha256: str


class MappingOut(BaseModel):
    rule_id: str
    label: str
    original_start: int
    original_end: int
    output_start: int
    output_end: int
    original_length: int
    replaced_length: int
    uncertain: bool
    original_sha256: str
    reason: Optional[str] = None


class RedactResponse(BaseModel):
    request_id: str
    rule_profile: str
    rule_version: str
    rule_fingerprint: str
    redacted: str
    mappings: list[MappingOut]
    uncertain: list[UncertainOut]
    input_length: int
    output_length: int


class SessionOpenResponse(BaseModel):
    request_id: str
    session_id: str
    rule_profile: str
    rule_version: str
    rule_fingerprint: str


class StreamChunkRequest(BaseModel):
    session_id: str
    profile: str = Field(default="standard")
    chunk: str = Field(..., description="Log chunk (synthetic data only)")
    final: bool = False


class StreamChunkResponse(BaseModel):
    request_id: str
    session_id: str
    chunk_index: int
    emitted: str
    final: bool
    mappings: list[MappingOut]
    uncertain: list[UncertainOut]
    input_length: int
    output_length: int
    closed: bool


class AuditFragmentOut(BaseModel):
    fragment_id: int
    rule_id: str
    label: str
    original_start: int
    original_end: int
    output_start: int
    output_end: int
    original_length: int
    replaced_length: int
    uncertain: bool
    reason: Optional[str]
    original_sha256: str
    original: Optional[str] = None


class ErrorEnvelope(BaseModel):
    request_id: str
    error_category: str
    message: str
