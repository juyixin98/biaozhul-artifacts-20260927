"""Pydantic 数据契约：模块间与 HTTP 边界共享的结构。

字段命名即契约；新增字段保持可选，禁止改变既有字段语义。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .config import LIMITS


# --------------------------------------------------------------------------- #
# 入参
# --------------------------------------------------------------------------- #
class SourceIn(BaseModel):
    text: str = Field(..., description="规范 Unicode 文本（码点序列）")
    media_type: str = Field("text/plain", description="仅作标注，服务统一按 UTF-8 处理")


class RuleIn(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=LIMITS.max_rule_id_chars)
    pattern: str = Field(..., max_length=LIMITS.max_pattern_chars,
                         description="可为空串（合法的零宽模式）")
    template: str = Field(..., max_length=LIMITS.max_template_chars)
    flags: str = Field("", description="受支持标志子集，可含 i/s/m，如 'im'")
    # 同轮优先级：数字小者优先；同优先级保持声明顺序（稳定）
    priority: int = Field(100, ge=0, le=1_000_000)
    strict_captures: bool = Field(
        True, description="True 时引用未参与的可选组直接失败；False 渲染为空串"
    )


class PlanRequest(BaseModel):
    source_id: str
    rules: list[RuleIn] = Field(..., min_length=1, max_length=LIMITS.max_rules_per_plan)
    dry_run: bool = Field(False, description="True 时只计算并返回统计，不持久化计划")


# --------------------------------------------------------------------------- #
# 出参 / 持久化表示
# --------------------------------------------------------------------------- #
class SourceVersionOut(BaseModel):
    source_id: str
    version: int
    spec: "SourceSpecOut"


class SourceSpecOut(BaseModel):
    sha256: str
    byte_len: int
    char_len: int


class GroupOut(BaseModel):
    index: int
    name: str | None
    text: str | None
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int


class ReplacementOut(BaseModel):
    index: int
    rule_id: str
    priority: int
    declaration_order: int
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    matched: str
    replacement: str
    zero_width: bool
    groups: list[GroupOut] = Field(default_factory=list)


class PlanSummary(BaseModel):
    plan_id: str
    source_id: str
    source_version: int
    source_spec: SourceSpecOut
    rule_count: int
    replacement_count: int
    zero_width_count: int
    status: Literal["planned", "applied"]
    applied_version: int | None = None


class PlanDetail(PlanSummary):
    rules: list[RuleIn]
    replacements: list[ReplacementOut]
    displaced: list["DisplacedHitOut"] = Field(default_factory=list)


class DisplacedHitOut(BaseModel):
    """诊断：产生了命中但因同轮重叠消解而未入选的匹配。"""

    rule_id: str
    char_start: int
    char_end: int
    reason: Literal["covered_by_higher_priority", "covered_by_earlier_same_priority"]


class ApplyResult(BaseModel):
    plan_id: str
    source_id: str
    source_version: int
    output_version: int
    output_spec: SourceSpecOut
    char_len: int
    replaced: int
    stream: bool = False


class RuleValidationOut(BaseModel):
    rule_id: str
    ok: bool
    group_count: int
    group_names: list[str]
    reason: str | None = None


class DiagnosticEventOut(BaseModel):
    seq: int
    run_id: str
    stage: str
    level: str
    event: str
    message: str
    data: dict


class DiagnosticsOut(BaseModel):
    run_id: str
    events: list[DiagnosticEventOut]


SourceVersionOut.model_rebuild()
PlanDetail.model_rebuild()
