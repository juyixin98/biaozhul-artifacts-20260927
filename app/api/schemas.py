"""Pydantic request/response models (strict, no coercing of odd shapes)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TYPE_LITERAL = Literal["text", "int", "bool", "decimal", "date", "timestamp"]
STATE_LITERAL = Literal["present", "null", "missing"]


class FieldDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    type: TYPE_LITERAL
    value_space: int | None = Field(default=None, ge=1)


class CreateBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=200)
    fields: list[FieldDeclaration] = Field(min_length=1)
    # Cells are heterogeneous JSON scalars/objects, validated by the parser.
    records: list[dict[str, Any]] = Field(min_length=1)


class DiscloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1)
    record_index: int = Field(ge=0)
    path: str = Field(min_length=1)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proof: dict[str, Any]
    trusted_batch_root_hex: str | None = None
    expected_path: str | None = None
    expected_record_index: int | None = Field(default=None, ge=0)
