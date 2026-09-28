"""Pydantic models for the audit HTTP interface."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class ReviewRequest(BaseModel):
    sql: str = Field(..., min_length=1, description="SQL template to review")
    parameters: dict[str, Any] | list[Any] | None = Field(
        default=None,
        description="Value bindings: dict for named/numbered, list for '?'.",
    )
    identifiers: dict[str, str] | None = Field(
        default=None,
        description="Bindings for ${slot} dynamic identifiers.",
    )

    @field_validator("sql")
    @classmethod
    def _sql_must_be_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("sql must not be blank")
        return v


class FindingModel(BaseModel):
    code: str
    severity: str
    message: str
    span: dict | None = None
    detail: dict = Field(default_factory=dict)


class ReviewResponse(BaseModel):
    verdict: str
    request_id: str
    sql_digest: str
    findings: list[FindingModel]
    bound_parameters: list[dict]
    identifier_bindings: list[dict]
    inert_occurrences: list[dict]
    statements: list[str]
    basis: dict
    limitations: list[str]
    diagnostics: dict
    stored: dict | None = None
