"""Pydantic wire models.

Shapes are deliberately permissive (``typing.Any`` for values/indices) so the
classified domain errors from the kernel/adapters — not Pydantic's generic
422 — explain type and range problems with batch/row context.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class BatchPayload(BaseModel):
    batch_id: str = Field(min_length=1)
    dictionary: list[Any]
    indices: list[int]
    # None => all valid at the adapter layer.
    validity: list[bool] | None = None

    model_config = {"extra": "forbid"}


class UnifyRequest(BaseModel):
    value_type: Literal["string", "int64", "double", "bool"]
    index_policy: Literal["auto", "strict"] = "auto"
    target_width: Literal[8, 16, 32] | None = None
    dedupe_local_dictionary: bool = False
    client_run_id: str | None = None
    batches: list[BatchPayload] = Field(min_length=1)

    model_config = {"extra": "forbid"}


class BatchRemapPayload(BaseModel):
    batch_id: str
    local_to_global: list[int]
    global_indices: list[int]
    validity: list[bool]
    row_count: int
    null_count: int


class NormalizationReport(BaseModel):
    batch_id: str
    duplicate_dictionary_entries: int
    duplicate_pairs: list[dict] = []


class UnifyResponse(BaseModel):
    job_id: str
    run_id: str
    global_dictionary: list[Any]
    global_value_type: str
    index_width_bits: int
    cardinality: int
    sort_policy: str
    batch_remaps: list[BatchRemapPayload]
    stats: dict
    normalization: list[NormalizationReport]
    versions: dict[str, str]


class ErrorResponse(BaseModel):
    error: dict
    job_id: str | None = None
    run_id: str | None = None


# ---- /verify payloads -------------------------------------------------------

class VerifyBatch(BaseModel):
    batch_id: str
    original_dictionary: list[Any]
    original_indices: list[int]
    local_to_global: list[int]
    global_indices: list[int]
    validity: list[bool]


class VerifyRequest(BaseModel):
    global_dictionary: list[Any]
    global_value_type: str
    index_width_bits: int
    sort_policy: str = "unknown"
    batches: list[VerifyBatch] = Field(min_length=1)
