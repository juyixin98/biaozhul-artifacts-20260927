"""HTTP/层间数据契约（pydantic）。

阈值默认线性幅度，也可通过 *_db 字段以 dBFS 提供（不能同时给）。
所有时长以毫秒给出，服务层经 timing 换算为样本数。
"""
from __future__ import annotations

import math
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class SegmentConfigIn(BaseModel):
    enter_threshold: Optional[float] = Field(
        default=0.03, ge=0.0, le=1.0,
        description="H->S 下限阈值（线性幅度）")
    exit_threshold: Optional[float] = Field(
        default=0.08, ge=0.0, le=1.0,
        description="S->H 上限阈值（线性幅度），须严格大于 enter")
    enter_threshold_db: Optional[float] = Field(default=None, le=0.0)
    exit_threshold_db: Optional[float] = Field(default=None, le=0.0)
    min_silence_ms: float = Field(default=200.0, ge=0.0)
    min_speech_ms: float = Field(default=50.0, ge=0.0)
    pad_before_ms: float = Field(default=10.0, ge=0.0)
    pad_after_ms: float = Field(default=20.0, ge=0.0)
    merge_gap_ms: float = Field(
        default=0.0, ge=0.0,
        description="相邻保留区间间隔 <= 该值则合并")
    edge_keep: bool = True

    @model_validator(mode="after")
    def _check(self) -> "SegmentConfigIn":
        db_used = self.enter_threshold_db is not None or \
            self.exit_threshold_db is not None
        if db_used and (self.enter_threshold_db is None
                        or self.exit_threshold_db is None):
            raise ValueError("enter/exit *_db must be provided together")
        # 默认值占位：显式给了任一线性阈值但又给 dB，视为歧义
        lin_explicit = self.model_fields_set  # pydantic 注入的已设字段集合
        if db_used and ("enter_threshold" in lin_explicit
                        or "exit_threshold" in lin_explicit):
            raise ValueError(
                "provide thresholds either linear or dB, not both")
        enter, exit_ = self.effective_thresholds()
        if not (enter < exit_):
            raise ValueError(
                f"require enter_threshold({enter}) < exit_threshold({exit_})")
        for name in ("min_silence_ms", "min_speech_ms", "pad_before_ms",
                     "pad_after_ms", "merge_gap_ms"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        return self

    def effective_thresholds(self) -> tuple[float, float]:
        if self.enter_threshold_db is not None:
            enter = 10.0 ** (self.enter_threshold_db / 20.0)
            exit_ = 10.0 ** (self.exit_threshold_db / 20.0)
            return enter, exit_
        return self.enter_threshold, self.exit_threshold  # type: ignore


class MediaSpec(BaseModel):
    container: str = Field(description="'pcm' 或 'wav'")
    sample_format: Optional[str] = None  # pcm 必填: s16/s24/s32/f32
    sample_rate: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    channels: Optional[int] = Field(default=None, ge=1, le=8)

    @model_validator(mode="after")
    def _check(self) -> "MediaSpec":
        if self.container not in ("pcm", "wav"):
            raise ValueError("container must be 'pcm' or 'wav'")
        if self.container == "pcm":
            if self.sample_format not in ("s16", "s24", "s32", "f32"):
                raise ValueError(
                    "sample_format required for pcm: s16/s24/s32/f32")
            if not self.sample_rate or not self.channels:
                raise ValueError(
                    "sample_rate and channels required for pcm container")
        return self


class CreateJob(BaseModel):
    config: SegmentConfigIn
    media: MediaSpec


class IntervalOut(BaseModel):
    start: int
    end: int
    start_ms: float
    end_ms: float


class JobStats(BaseModel):
    num_intervals: int
    kept_samples: int
    dropped_samples: int


class JobOut(BaseModel):
    job_id: str
    status: str
    container: str
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    total_samples: int
    duration_ms: Optional[float] = None
    intervals_committed: list[IntervalOut]
    all_intervals_final: bool
    stats: Optional[JobStats] = None
    error: Optional[dict] = None


class VerifyResult(BaseModel):
    job_id: str
    ok: bool
    checks: dict
    mismatch: Optional[dict] = None
