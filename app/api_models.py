"""Pydantic request/response models for the validation API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateTableRequest(BaseModel):
    table: str = Field(..., min_length=1, description="table name")
    partition_spec: list[str] = Field(
        default_factory=list, description="partition column names, in order"
    )


class CommitRequest(BaseModel):
    table: str = Field(..., min_length=1)
    operation: str = Field(..., description="APPEND or OVERWRITE")
    request_id: str | None = Field(
        None,
        description="client-supplied idempotency key (req-...); same id + "
        "same payload always returns the same outcome",
    )
    base_snapshot_id: int | None = Field(
        None, description="snapshot the client built the commit against"
    )
    files: list[str] = Field(..., min_length=1, description="local parquet paths")


class FileView(BaseModel):
    relpath: str
    source: str
    fingerprint: str
    row_count: int
    partition_keys: list[str]


class CommitResponse(BaseModel):
    request_id: str
    table: str
    operation: str
    status: str
    base_snapshot_id: int
    head_before_snapshot_id: int
    snapshot_id: int | None
    rebased: bool
    replayed: bool
    attempts: int
    files: list[FileView]
    total_row_count: int
    partition_keys: list[str]
    error_category: str | None = None
    error_message: str | None = None


class ErrorBody(BaseModel):
    error: str
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class SnapshotView(BaseModel):
    snapshot_id: int
    parent_snapshot_id: int | None
    operation: str
    commit_id: str
    created_at: float
    row_count: int
    files: list[FileView]


class CommitLogView(BaseModel):
    request_id: str
    table: str
    operation: str | None
    status: str
    base_snapshot_id: int | None
    final_snapshot_id: int | None
    rebased: bool
    attempts: int
    error_category: str | None
    error_message: str | None


class CleanupRecordView(BaseModel):
    record_id: str
    request_id: str
    table_name: str
    kind: str
    path: str
    status: str
    error: str | None
