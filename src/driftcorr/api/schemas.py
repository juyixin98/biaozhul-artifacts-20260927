"""API request/response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class JobCreateRequest(BaseModel):
    reference_path: str = Field(..., description="WAV file on the reference clock")
    target_path: str = Field(..., description="WAV file on the drifting clock")
    reference_metadata_path: str = Field(
        ..., description="JSON sidecar declaring sync pulse times on the reference")
    target_metadata_path: str | None = Field(
        None, description="optional JSON sidecar with events to time-map")


class JobSummary(BaseModel):
    job_id: str
    request_id: str
    status: str
    created_at: str
    updated_at: str
    error_class: str | None = None
    error_message: str | None = None
    pipeline_version: str


class JobDetail(JobSummary):
    params: dict[str, Any]
    result: dict[str, Any] | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    pipeline_version: str
