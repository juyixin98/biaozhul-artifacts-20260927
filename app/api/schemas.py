"""Pydantic request/response models for the HTTP boundary."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceIn(BaseModel):
    text: str = Field(description="Document text. Uploaded as a JSON string; "
                                  "the service stores its UTF-8 bytes.")
    normalize_newlines: bool = False


class SourceOut(BaseModel):
    id: str
    version: int
    sha256: str
    length: int
    codepoints: int | None = None
    normalize_newlines: bool


class RuleIn(BaseModel):
    rule_id: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    template: str
    priority: int = 0
    flags: str = ""
    longest_match: bool = False
    max_mem: int = 8 * 1024 * 1024
    missing_capture: Literal["error", "empty"] = "error"


class RulesetIn(BaseModel):
    rules: list[RuleIn] = Field(min_length=1)


class RulesetOut(BaseModel):
    id: str
    version: int
    rule_count: int


class PlanOut(BaseModel):
    plan_id: str
    source_sha256: str
    source_length: int
    ruleset_id: str
    edit_count: int
    candidates_total: int
    candidates_dropped: int


class EditOut(BaseModel):
    start: int
    end: int
    zero_width: bool
    rule_id: str
    matched: str
    replacement: str


class PlanDetailOut(PlanOut):
    edits: list[EditOut]
    decisions: list[dict]


class ApplyIn(BaseModel):
    source_id: str | None = Field(
        default=None,
        description="Apply against this logical source id. Its current digest "
                    "must match the plan binding; otherwise 409.",
    )
    expected_sha256: str | None = Field(
        default=None,
        description="Optional client-side expected digest; mismatch -> 409.",
    )
    save_result_as: str | None = None


class ApplyOut(BaseModel):
    application_id: str
    plan_id: str
    source_id: str
    expected_sha256: str
    result_sha256: str
    result_length: int
    chunks_emitted: int
    new_source_id: str | None
    output: str
