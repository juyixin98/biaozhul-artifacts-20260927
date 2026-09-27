"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AlignRequest(BaseModel):
    reference_path: str | None = Field(
        default=None, description="Path to reference (master) mono WAV file")
    slave_path: str | None = Field(
        default=None, description="Path to slave mono WAV file")
    stereo_path: str | None = Field(
        default=None,
        description="Alternative: one stereo WAV with reference+slave channels")
    mode: Literal["pulses", "correlate", "auto"] | None = None
    external_time_anchor: bool = Field(
        default=False,
        description="True only when an external absolute-time anchor exists; "
                    "controls whether offset may be read as absolute time")


class JobSummary(BaseModel):
    job_id: str
    request_id: str
    status: str
    submitted_at: float
    updated_at: float


class JobCreated(BaseModel):
    job_id: str
    request_id: str
    status: str


class Event(BaseModel):
    ts: float
    seq: int
    step: str
    status: str
    detail: dict[str, Any]


class JobResponse(BaseModel):
    job_id: str
    request_id: str
    status: str
    version: str
    config_source: str
    submitted_at: float
    updated_at: float
    failure: dict | None
    events: list[Event]
    report: dict | None


class ValidationRequest(BaseModel):
    job_id: str
    # Optional independently generated fixture truth.
    truth: dict | None = None


class ValidationResponse(BaseModel):
    job_id: str
    request_id: str
    passed: bool
    checks: list[dict]
    failure_categories: list[str]
