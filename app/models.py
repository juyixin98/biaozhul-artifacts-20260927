"""API 数据模型层 —— 显式声明字段角色，NULL 不被悄悄丢弃。

角色（role）必须逐列显式声明：
* ``quasi_identifier`` —— 参与等价类划分与泛化；
* ``sensitive``        —— 敏感属性，参与 l-多样性；
* ``insensitive``      —— 明确保留但不参与隐私计算；
* 不允许"未声明"的列进入分析（防止调用方误以为某列被保护）。

NULL 表示规则
-------------
输入值为 ``null``、空字符串或 Python ``None`` 时，规范化为哨兵
``NULL_VALUE``，仍然留在样本中（参与计数与等价类划分）。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

NULL_VALUE = "\x00__NULL__"  # 规范化后的 NULL 哨兵（极不可能与真实值冲突）
NAME_RE = re.compile(r"^[A-Za-z0-9_一-鿿][A-Za-z0-9_.一-鿿\- ]{0,63}$")


def canonical(value: Any) -> str:
    """把任意标量输入规范化为可分组的字符串；None/空串 → NULL 哨兵。"""
    if value is None:
        return NULL_VALUE
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # 整数浮点规范化：20.0 与 20 视为同值
        if value.is_integer():
            return str(int(value))
        return repr(value)
    s = str(value).strip()
    return NULL_VALUE if s == "" else s


def is_null(value: str) -> bool:
    return value == NULL_VALUE


class ColumnRole(str, Enum):
    QUASI_IDENTIFIER = "quasi_identifier"
    SENSITIVE = "sensitive"
    INSENSITIVE = "insensitive"


class HierarchySpec(BaseModel):
    """泛化层级声明。

    ``levels`` 为层级列表，第 0 层为恒等映射（原始值→原始值），
    后续每层把值映射到更粗粒度。NULL 在所有层级恒等传播。

    包含关系（单调性）在 :mod:`app.core.hierarchies` 中做强校验：
    第 h+1 层的每个分组必须是第 h 层若干分组的并集。
    """

    levels: list[dict[str, str]] = Field(
        ...,
        min_length=1,
        description="层级映射；levels[0] 必须是恒等映射（覆盖全部原始值）",
    )

    @field_validator("levels")
    @classmethod
    def _levels_shape(cls, v: list[dict[str, str]]) -> list[dict[str, str]]:
        if not v:
            raise ValueError("hierarchy must have at least level 0")
        return v


class ColumnSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    role: ColumnRole
    hierarchy: HierarchySpec | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not NAME_RE.match(v):
            raise ValueError(
                "column name must be 1-64 chars: letters/digits/underscore/中文/点/横线"
            )
        return v

    @model_validator(mode="after")
    def _check_hierarchy_role(self) -> "ColumnSpec":
        if self.role is ColumnRole.QUASI_IDENTIFIER and self.hierarchy is None:
            raise ValueError(
                f"quasi_identifier column '{self.name}' requires a hierarchy "
                "(level 0 identity mapping is the minimum)"
            )
        if self.role is not ColumnRole.QUASI_IDENTIFIER and self.hierarchy is not None:
            raise ValueError(
                f"hierarchy is only allowed on quasi_identifier columns "
                f"(column '{self.name}' role={self.role.value})"
            )
        return self


class DatasetIn(BaseModel):
    """一次分析提交：列声明 + 行数据 + 阈值。"""

    name: str = Field(default="synthetic", max_length=128)
    columns: list[ColumnSpec] = Field(..., min_length=1)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    k: int = Field(default=2, ge=1, le=10_000)
    l: int = Field(default=1, ge=1, le=10_000)
    # 当阈值无法满足时是否仍返回最优尝试（默认 False：明确失败而非伪成功）
    fail_on_unreachable: bool = True
    note: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def _check_thresholds(self) -> "DatasetIn":
        if self.k < 2:
            # k=1 没有匿名意义，明确拒绝而非静默放行
            raise ValueError("k must be >= 2 (k=1 provides no anonymity)")
        if self.l < 1:
            raise ValueError("l must be >= 1")
        if self.l > self.k:
            raise ValueError(
                f"l ({self.l}) cannot exceed k ({self.k}): a class of k<l rows "
                "cannot hold l distinct sensitive values"
            )
        return self


class RunRequest(BaseModel):
    schema_id: str = Field(..., min_length=1)
    k: int = Field(default=2, ge=2)
    l: int = Field(default=1, ge=1)
    fail_on_unreachable: bool = True

    @model_validator(mode="after")
    def _check(self) -> "RunRequest":
        if self.l > self.k:
            raise ValueError("l cannot exceed k")
        return self


# --------------------------------------------------------------------------- #
# 输出模型
# --------------------------------------------------------------------------- #


class EquivalenceClassOut(BaseModel):
    """等价类输出：只暴露风险所需信息，不回显真实准标识符/敏感值。"""

    class_index: int
    size: int
    distinct_sensitive: int
    max_sensitive_frequency: int
    meets_k: bool
    meets_l: bool
    risk_level: str  # "high" | "medium" | "low"
    contains_null_qi: bool


class LevelsOut(BaseModel):
    column: str
    level: int


class RunSummary(BaseModel):
    n_rows: int
    n_classes: int
    min_class_size: int
    max_class_size: int
    classes_below_k: int
    classes_below_l: int
    rows_in_below_k_classes: int
    rows_with_any_null_qi: int
    fraction_identifiable: float


class RunResponse(BaseModel):
    run_id: str
    schema_id: str
    status: str  # succeeded | k_unreachable | l_unreachable | failed
    failure_code: str | None = None
    failure_category: str | None = None
    message: str
    k: int
    l: int
    chosen_levels: list[LevelsOut] | None = None
    info_loss: float | None = None
    info_loss_metric: str | None = None
    n_combinations_explored: int
    n_suppressed: int = 0
    summary: RunSummary | None = None
    equivalence_classes: list[EquivalenceClassOut] | None = None
    classes_truncated: bool = False
    null_kept_in_sample: bool
    n_null_rows: int
    computation_trace: list[dict[str, Any]] | None = None
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = (
        "k-anonymity and l-diversity are syntactic heuristics against specific "
        "re-identification/attribute-disclosure attacks; they do NOT constitute a "
        "complete privacy guarantee (see README §限制)."
    )
    service_version: str
