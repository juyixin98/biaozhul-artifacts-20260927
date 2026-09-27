"""请求/响应数据模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class RawPacketIn(BaseModel):
    arrival_us: int = Field(..., description="到达时刻（接收端微秒）")
    rtp_base64: Optional[str] = Field(None, description="RTP 报文 base64")
    rtp_hex: Optional[str] = Field(None, description="或 RTP 报文 hex")
    encoding: str = Field("base64", description="base64 | hex")


class PlannerParams(BaseModel):
    clock_rate: Optional[int] = None
    samples_per_packet: Optional[int] = None
    min_delay_us: Optional[int] = None
    max_delay_us: Optional[int] = None
    jitter_multiplier: Optional[float] = None
    jitter_smoothing: Optional[float] = None
    drift_smoothing: Optional[float] = None
    delay_persistence: Optional[float] = None
    drift_warmup_packets: Optional[int] = None
    fixed_delay_us: Optional[int] = None
    max_buffer_packets: Optional[int] = None


class RawSimRequest(BaseModel):
    packets: list[RawPacketIn] = Field(..., min_length=1)
    params: Optional[PlannerParams] = None
    expected_clock_ratio: Optional[float] = None


class ScenarioRequest(BaseModel):
    scenario: str
    fixed_delay_us: Optional[int] = None


class JobRef(BaseModel):
    job_id: str
    status: str
    location: str


class ErrorBody(BaseModel):
    error_code: str
    message: str
    request_id: str
    details: Any = None
