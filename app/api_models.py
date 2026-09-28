"""Pydantic request/response models for the validation API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValidateRequest(BaseModel):
    request_id: str | None = Field(
        default=None,
        description="Client-chosen correlation id; a UUID is generated if absent")
    schema_: dict[str, Any] = Field(alias="schema", description="Restricted schema")
    records: list[dict[str, Any]] = Field(default_factory=list)
    expected_tree: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional hand-written expected record tree for an exact "
                    "assertion in addition to the PyArrow cross-check")
    page_size_bytes: int | None = Field(
        default=None, ge=1,
        description="Target data page size; pages never split a record")
    force_page_after_records: int | None = Field(
        default=None, ge=1,
        description="Test aid: force a page boundary every N records")

    model_config = {"populate_by_name": True}


class StepDetail(BaseModel):
    name: str
    status: str
    detail: dict[str, Any] | None = None


class Mismatch(BaseModel):
    category: str
    record_index: int
    column_path: str | None = None
    page_index: int | None = None
    position: int | None = None
    expected: Any = None
    actual: Any = None
    message: str


class ValidateResponse(BaseModel):
    request_id: str
    status: str  # passed | failed | error
    record_count: int
    page_count: int
    steps: list[StepDetail]
    mismatches: list[Mismatch]
    # Conclusions the system cannot make with certainty, kept separate from
    # hard failures (e.g. an empty struct whose presence is physically
    # invisible in Parquet).
    uncertainties: list[str]
    warnings: list[str]
    artifact: dict[str, Any] | None = None
    error_category: str | None = None
    error_message: str | None = None


class StatusResponse(BaseModel):
    request_id: str
    status: str
    created_at: str
    record_count: int
    error_category: str | None = None
    error_detail: str | None = None
    steps: list[StepDetail]
    warnings: list[Any]
    artifact: dict[str, Any] | None = None
