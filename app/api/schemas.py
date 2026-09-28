"""Pydantic request/response models for the audit API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.version import (
    COMMITMENT_SCHEMA_VERSION,
    DISCLOSURE_SCHEMA_VERSION,
    MERKLE_SCHEMA_VERSION,
)


class FieldSpecIn(BaseModel):
    name: str = Field(min_length=1)
    type: Literal["string", "int", "decimal", "bool", "date", "timestamp", "null"]
    salted: bool = True


class CreateBatchIn(BaseModel):
    schema_: list[FieldSpecIn] = Field(alias="schema", min_length=1)
    records: list[dict[str, Any]] = Field(min_length=1)

    model_config = {"populate_by_name": True}


class CreateBatchOut(BaseModel):
    batch_id: str
    root_hex: str
    record_count: int
    schema_fields: list[dict] = Field(alias="schema")
    commitments: list[dict]
    warnings: list[str]
    commitment_schema_version: str = COMMITMENT_SCHEMA_VERSION
    merkle_schema_version: str = MERKLE_SCHEMA_VERSION

    model_config = {"populate_by_name": True}


class SelectorIn(BaseModel):
    record_index: int = Field(ge=0)
    field_name: str = Field(min_length=1)


class DiscloseIn(BaseModel):
    selectors: list[SelectorIn] = Field(min_length=1)


class VerifyIn(BaseModel):
    package: dict[str, Any]


class ItemVerdict(BaseModel):
    record_index: int
    field_name: str
    verdict: str
    detail: dict[str, Any] = Field(default_factory=dict)


class VerifyOut(BaseModel):
    verdict: str
    valid: bool
    root_hex: str | None
    recomputed_root_hex: str | None
    items: list[ItemVerdict]
    errors: list[str]


class AuditEntryOut(BaseModel):
    seq: int
    ts: str
    run_id: str
    batch_id: str | None
    action: str
    verdict: str
    detail: dict[str, Any]
