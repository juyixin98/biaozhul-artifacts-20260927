"""Pydantic request models for the verification interface."""
from __future__ import annotations

from pydantic import BaseModel, Field


class BufferDescriptor(BaseModel):
    type: str = Field(description="Arrow type name, e.g. int32 / utf8")
    length: int = Field(ge=0, description="logical element count")
    data: str = Field(description="base64 data (values) buffer")
    validity: str | None = Field(default=None, description="base64 validity bitmap (omit = all valid)")
    offsets: str | None = Field(default=None, description="base64 int32 offsets buffer (utf8 only)")
    null_count: int | None = Field(default=None, description="claimed null count; checked against bitmap")


class SliceRequest(BaseModel):
    offset: int = Field(ge=0)
    length: int = Field(ge=0)


class ConcatRequest(BaseModel):
    column_ids: list[str] = Field(min_length=1)
    target_type: str | None = Field(
        default=None,
        description="required when inputs differ in type; performs an explicit cast",
    )


class IpcImportRequest(BaseModel):
    ipc_stream_b64: str = Field(description="base64 Arrow IPC streaming message")
    column_index: int = Field(default=0, ge=0)
