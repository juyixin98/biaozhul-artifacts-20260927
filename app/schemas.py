"""API 层的请求/响应模型（Pydantic）。

对外时间量一律毫秒；由 service 层调用 timing 内核换算成采样点。
阈值缺省值来自 Settings（注入，避免模型读全局）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConfigPayload(BaseModel):
    sample_rate: int | None = Field(default=None, ge=1, le=1_000_000)
    fmt: str | None = Field(default="auto")
    min_silence_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    min_activity_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    pad_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    merge_gap_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    enter_threshold: float | None = Field(default=None, ge=0, le=1_000.0)
    exit_threshold: float | None = Field(default=None, ge=0, le=1_000.0)

    def resolved(self, defaults: dict[str, Any]) -> dict[str, Any]:
        """与服务默认值合并，返回 service 层使用的扁平字典。"""
        return {
            "sample_rate": self.sample_rate,
            "fmt": self.fmt or "auto",
            "min_silence_ms": self.min_silence_ms
            if self.min_silence_ms is not None
            else defaults["min_silence_ms"],
            "min_activity_ms": self.min_activity_ms
            if self.min_activity_ms is not None
            else defaults["min_activity_ms"],
            "pad_ms": self.pad_ms
            if self.pad_ms is not None
            else defaults["pad_ms"],
            "merge_gap_ms": self.merge_gap_ms
            if self.merge_gap_ms is not None
            else defaults["merge_gap_ms"],
            "enter_threshold": self.enter_threshold
            if self.enter_threshold is not None
            else defaults["enter_threshold"],
            "exit_threshold": self.exit_threshold
            if self.exit_threshold is not None
            else defaults["exit_threshold"],
        }


class VerifyRequest(BaseModel):
    """验证接口入参：给一段合成信号与参数，返回守恒/不变性等核验结果。"""

    # 信号用普通 JSON 数字数组（合成夹具量级，测试够用）。
    samples: list[float]
    sample_rate: int = Field(ge=1, le=1_000_000)
    enter_threshold: float = Field(ge=0)
    exit_threshold: float = Field(ge=0)
    min_silence_ms: int = Field(ge=0)
    min_activity_ms: int = Field(ge=0)
    pad_ms: int = Field(default=0, ge=0)
    merge_gap_ms: int = Field(default=0, ge=0)
    chunk_sizes: list[int] = Field(default_factory=list)
