"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DimensionIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    bits: int = Field(ge=1, le=64)
    signed: bool = False


class SchemaIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    dimensions: list[DimensionIn] = Field(min_length=1, max_length=32)


class BoxEdgeIn(BaseModel):
    dimension: str
    lo: int
    hi: int


class QueryIn(BaseModel):
    box: list[BoxEdgeIn] = Field(min_length=1)
    interval_budget: int | None = Field(default=None, ge=1)


class IngestIn(BaseModel):
    rows: list[dict[str, int]] = Field(min_length=1)


class ErrorOut(BaseModel):
    request_id: str
    error_category: str
    message: str
    detail: dict | None = None
    uncertainties: list[str] = Field(default_factory=list)
