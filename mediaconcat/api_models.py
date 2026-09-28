"""HTTP API 模式（与内部模型解耦）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CutSpec(BaseModel):
    cut_in_sec: Optional[float] = Field(None, ge=0)
    cut_out_sec: Optional[float] = Field(None, ge=0)


class ConcatRequest(BaseModel):
    sources: list[str] = Field(..., min_length=1,
                               description="片段标识：fixtures 下的夹具名或 sidecar 路径")
    output_container: str = Field("mp4", pattern="^(mp4|fmp4|mov|mkv|mpegts|ts|m2ts)$")
    cuts: Optional[list[CutSpec]] = None
    job_id: Optional[str] = Field(None, min_length=1, max_length=64)


class JobSummary(BaseModel):
    job_id: str
    run_id: str
    status: str
    container: str
    error_code: Optional[str] = None
    created_at: float
    updated_at: float


class JobDetail(BaseModel):
    job_id: str
    run_id: str
    status: str
    container: str
    sources: list[str]
    error_code: Optional[str] = None
    error: Optional[str] = None
    plan: Optional[dict] = None
