"""API 请求/响应模型（Pydantic v2）。

输入层级用判别联合（按 ``rule`` 字段区分 map/prefix/range），在 HTTP 边界
提前给出 422；内核解析层保留独立校验，使内核可脱离 Web 使用。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class MapLevelIn(BaseModel):
    rule: Literal["map"]
    mapping: dict[str, str] = Field(min_length=1)
    name: Optional[str] = None


class PrefixLevelIn(BaseModel):
    rule: Literal["prefix"]
    keep: int = Field(ge=1)
    name: Optional[str] = None


class RangeLevelIn(BaseModel):
    rule: Literal["range"]
    bins: list[float] = Field(min_length=2)
    labels: Optional[list[str]] = None
    name: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        if any(self.bins[i] >= self.bins[i + 1] for i in range(len(self.bins) - 1)):
            raise ValueError("bins 必须严格递增")
        if self.labels is not None and len(self.labels) != len(self.bins) - 1:
            raise ValueError("labels 数量必须等于箱数（len(bins)-1）")
        return self


LevelIn = Annotated[
    Union[MapLevelIn, PrefixLevelIn, RangeLevelIn],
    Field(discriminator="rule"),
]


class HierarchyIn(BaseModel):
    levels: list[LevelIn] = Field(default_factory=list)


class CreateRunIn(BaseModel):
    columns: list[str] = Field(min_length=1)
    rows: list[list[Optional[str]]] = Field(min_length=1)
    quasi_identifiers: list[str] = Field(min_length=1, alias="quasi_identifiers")
    sensitive: list[str] = Field(min_length=1)
    hierarchies: dict[str, HierarchyIn]

    model_config = {"populate_by_name": True}


class ThresholdIn(BaseModel):
    k: int = Field(ge=1)
    l: int = Field(ge=1)


class LevelsIn(BaseModel):
    levels: dict[str, int]


class SuggestIn(ThresholdIn):
    pass


class RunOut(BaseModel):
    run_id: str
    access_token: str
    created_at: str
    row_count: int
    quasi_identifiers: list[str]
    sensitive: list[str]
    null_counts: dict[str, int]
    hierarchy_validation: list[dict[str, Any]]
    metric_version: str


class HealthOut(BaseModel):
    status: str
    service: str
    version: str
    metric_version: str
    key_ephemeral: bool
    config_source: Optional[str]
    run_count: int
