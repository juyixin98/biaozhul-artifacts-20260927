"""Pydantic response models for the HTTP API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class JobCreated(BaseModel):
    job_id: str
    status: str = "queued"


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    input_name: Optional[str] = None
    input_size: Optional[int] = None
    input_sha256: Optional[str] = None
    error: Optional[str] = None


class JobSummary(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    input_name: Optional[str] = None
    input_size: Optional[int] = None


class EventResponse(BaseModel):
    seq: int
    code: str
    severity: str
    message: str
    pid: Optional[int] = None
    offset: Optional[int] = None
    context: dict[str, Any] = Field(default_factory=dict)


class ValidateResponse(BaseModel):
    record_id: str
    verdict: str            # "accepted" | "rejected" | "indeterminate"
    reason: str
    fatal: Optional[str]
    severity_counts: dict[str, int]
    error_count: int
    warning_count: int
    packets_parsed: int
    bytes_skipped: int
    report: dict[str, Any]


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "mpeg-ts-analyzer"
