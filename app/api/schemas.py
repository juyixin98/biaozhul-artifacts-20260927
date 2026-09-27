"""Pydantic request/response models for the validation API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ValidateRequest(BaseModel):
    content: str = Field(..., description="Subtitle document as UTF-8 text")
    format: Literal["srt", "vtt"] | None = Field(
        None, description="Force a parser; auto-detected from content when omitted"
    )


class DiagnosticModel(BaseModel):
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    cue_index: int | None = None
    other_index: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class CueRepairModel(BaseModel):
    cue_index: int
    original_start_ms: int
    original_end_ms: int
    repaired_start_ms: int
    repaired_end_ms: int
    shift_ms: int
    action: str
    reasons: list[str]


class RepairModel(BaseModel):
    status: str
    message: str
    total_shift_ms: int
    max_shift_ms: int
    budget_ms: int
    cues: list[CueRepairModel]


class ValidationResponse(BaseModel):
    run_id: str
    status: Literal["clean", "repaired", "parse_failed",
                    "infeasible_bounds", "budget_exceeded", "solver_too_large"]
    format: str
    cue_count: int
    message: str
    elapsed_ms: float
    failure_codes: list[str]
    diagnostics: list[DiagnosticModel]
    repair: RepairModel | None = None
    repaired_document: str | None = None


class JobSummary(BaseModel):
    job_id: str
    status: str
    fmt: str | None
    cue_count: int
    input_sha256: str
    error_message: str | None
    created_at: str
    updated_at: str
    result: dict[str, Any] | None = None
    repaired_document: str | None = None


class ErrorBody(BaseModel):
    error_code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
