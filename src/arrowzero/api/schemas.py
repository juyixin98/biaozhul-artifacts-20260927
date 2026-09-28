"""Pydantic request models for the validation interface."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImportRequest(BaseModel):
    format: str = Field(default="pylist", description="pylist | ipc_stream | raw_buffers")
    type: str | None = Field(default=None, description="Arrow type name, e.g. int32, utf8")
    values: list[Any] | None = None
    payload: str | None = Field(default=None, description="base64 IPC stream payload")
    # raw_buffers fault-injection descriptor
    length: int | None = None
    offset: int = 0
    buffers: list[str | None] | None = None
    check_utf8: bool = True

    def to_service_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class SliceRequest(BaseModel):
    handle: str
    offset: int = Field(ge=0)
    length: int | None = Field(default=None, ge=0)
    run_id: str | None = None


class ConcatRequest(BaseModel):
    handles: list[str] = Field(min_length=1)
    cast_to: str | None = None
    run_id: str | None = None


class HandleRequest(BaseModel):
    handle: str
    run_id: str | None = None


class ValidateRequest(BaseModel):
    type: str
    length: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)
    buffers: list[str | None]
    check_utf8: bool = True

    def to_descriptor(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "length": self.length,
            "offset": self.offset,
            "buffers": self.buffers,
            "check_utf8": self.check_utf8,
        }
