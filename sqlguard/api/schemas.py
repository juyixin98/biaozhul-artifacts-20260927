"""Pydantic request/response schemas for the audit API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    template: str = Field(..., description="Parameterized SQL template; "
                          "value params use ?/:name, dynamic identifiers "
                          "use {{ slot_name }}")
    params: dict[str, Any] | None = Field(
        None, description="Bind values keyed by '0','1',... or by param name. "
        "Values are never logged or stored verbatim.")
    slots: dict[str, str] | None = Field(
        None, description="Chosen identifiers for {{ slot }} placeholders")
    policy_overrides: dict[str, Any] | None = Field(
        None, description="Per-request inline slot/param whitelist additions")
    request_id: str | None = Field(None, description="Client correlation id")


class ReviewResponseBody(BaseModel):
    request_id: str
    verdict: str
    statement_type: str | None
    rendered_sql: str | None
    resolved_identifiers: dict[str, str]
    param_diagnostics: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    coverage: dict[str, Any]
    codes: dict[str, list[str]]


class AuditListEntry(BaseModel):
    seq: int
    request_id: str
    ts: float
    verdict: str
    statement_type: str | None


class ChainReportBody(BaseModel):
    ok: bool
    records: int
    first_bad_seq: int | None
    reason: str | None
