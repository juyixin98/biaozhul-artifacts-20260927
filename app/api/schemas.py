from typing import Literal, Optional

from pydantic import BaseModel, Field


class RepairOptions(BaseModel):
    min_duration_ms: int = Field(default=1000, gt=0)
    min_gap_ms: int = Field(default=0, ge=0)
    budget_ms: Optional[int] = Field(default=None, ge=0)
    resolution_ms: Optional[int] = Field(default=None, ge=1)
    allow_approximate: bool = False
    max_grid: Optional[int] = Field(default=None, ge=100)


class ValidateRequest(BaseModel):
    format: Literal["srt", "vtt"]
    content: str = Field(min_length=1)
    media_duration_ms: Optional[int] = Field(default=None, ge=0)
    options: RepairOptions = Field(default_factory=RepairOptions)
