"""Pydantic request/response contracts for the HTTP API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DesignRequest(BaseModel):
    fin: int = Field(..., gt=0, description="input sample rate Hz")
    fout: int = Field(..., gt=0, description="output sample rate Hz")
    attenuation_db: float = 80.0
    transition_half_width: float = 0.1


class CreateJobRequest(BaseModel):
    fin: int = Field(..., gt=0)
    fout: int = Field(..., gt=0)
    output_dtype: Literal["float64", "float32"] = "float64"


class SamplesPayload(BaseModel):
    encoding: Literal["json", "base64-float64-le"]
    data: Any


class ValidatePayloadRequest(BaseModel):
    fin: int = Field(..., gt=0)
    payload: SamplesPayload


class OfflineWavRequest(BaseModel):
    wav_base64: str
    fout: int = Field(..., gt=0)
    output_sample_width: Literal[2, 3, 4] = 2
    chunk_size: int | None = Field(default=None, ge=1)


class OfflineSamplesRequest(BaseModel):
    fin: int = Field(..., gt=0)
    fout: int = Field(..., gt=0)
    payload: SamplesPayload
    chunk_size: int | None = Field(default=None, ge=1)
    output_dtype: Literal["float64", "float32"] = "float64"
