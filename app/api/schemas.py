"""Pydantic response schemas for the HTTP interface."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FindingOut(BaseModel):
    code: str
    severity: str
    disposition: str
    message: str
    packet_index: int | None = None
    pid: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class JobSubmitted(BaseModel):
    job_id: str
    request_id: str
    status: str
    input_bytes: int


class JobStatus(BaseModel):
    job_id: str
    request_id: str
    status: str
    submitted_at: float
    started_at: float | None = None
    finished_at: float | None = None
    input_name: str | None = None
    input_bytes: int
    error: str | None = None
    report: dict[str, Any] | None = None
