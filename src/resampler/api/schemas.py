"""Pydantic request/response models for the HTTP boundary."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValidateRequest(BaseModel):
    input_rate: int = Field(gt=0, description="input sample rate in Hz, integer")
    output_rate: int = Field(gt=0, description="output sample rate in Hz, integer")
    attenuation_db: float | None = Field(default=None, gt=0)
    passband_edge: float | None = Field(default=None, gt=0, lt=1)


class GroupDelay(BaseModel):
    high_rate_samples: float
    input_samples: float
    output_samples: float


class ValidateResponse(BaseModel):
    up: int
    down: int
    taps_per_phase: int
    num_taps: int
    attenuation_db: float
    passband_edge_fraction: float
    passband_edge_hz: float
    stopband_edge_hz: float
    cutoff_hz: float
    group_delay: GroupDelay
    padding: dict[str, Any]
    note: str


class CreateJobRequest(BaseModel):
    input_rate: int = Field(gt=0)
    output_rate: int = Field(gt=0)
    input_format: str = "f64le"
    input_container: str = Field(default="raw", pattern="^(raw|wav)$")
    output_format: str = "f64le"
    output_container: str = Field(default="raw", pattern="^(raw|wav)$")
    clip_policy: str = Field(default="clip", pattern="^(clip|reject)$")
    attenuation_db: float | None = Field(default=None, gt=0)
    passband_edge: float | None = Field(default=None, gt=0, lt=1)
    job_id: str | None = Field(default=None, min_length=1, max_length=64)


class JobResponse(BaseModel):
    job_id: str
    state: str
    plan: dict[str, Any]
    input_format: str
    input_container: str
    output_format: str
    output_container: str
    clip_policy: str
    run_id: str


class ChunkResponse(BaseModel):
    job_id: str
    chunk_index: int
    input_samples: int
    output_samples_emitted: int
    clipped_samples: int
    total_input_samples: int
    total_output_samples: int
    state: str
    run_id: str


class FlushResponse(BaseModel):
    job_id: str
    state: str
    tail_samples: int
    total_input_samples: int
    total_output_samples: int
    clipped_samples: int
    run_id: str


class StatusResponse(BaseModel):
    job_id: str
    state: str
    input_samples: int
    output_samples: int
    clipped_samples: int
    chunks_received: int
    error: dict[str, Any] | None = None
    plan: dict[str, Any]


class ChunkJsonRequest(BaseModel):
    data_b64: str = Field(description="base64 payload, raw PCM or complete WAV bytes")
