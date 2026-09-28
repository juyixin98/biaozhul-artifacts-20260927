"""核心数据模型（纯 dataclass，不依赖 Web 框架）。

裁剪决策全部带 ``PruneReason`` 原因码与人类可读解释，满足
"被裁剪文件不可能匹配的理由必须可解释"。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------- 谓词

class PredicateKind(str, enum.Enum):
    RANGE = "range"          # 半开/闭区间
    IS_NULL = "is_null"
    NOT_NULL = "not_null"
    EQ = "eq"
    IN = "in"


@dataclass(frozen=True)
class Predicate:
    """针对单个列的谓词。

    - kind=range: lower/upper 给出区间边界，lower_inclusive/upper_inclusive 决定开闭；
      None 端表示无界。值可以是 int/float（epoch 秒）或日期字符串 'YYYY-MM-DD'。
    - kind=eq/in: value / values。
    - kind=is_null/not_null: 无值。
    """
    column: str
    kind: PredicateKind
    value: Any = None
    values: Optional[tuple] = None
    lower: Any = None
    upper: Any = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True

    @staticmethod
    def range_(column, lower=None, upper=None, lower_inclusive=True, upper_inclusive=False):
        return Predicate(column=column, kind=PredicateKind.RANGE, lower=lower, upper=upper,
                         lower_inclusive=lower_inclusive, upper_inclusive=upper_inclusive)

    @staticmethod
    def eq(column, value):
        return Predicate(column, PredicateKind.EQ, value=value)

    @staticmethod
    def in_(column, values):
        return Predicate(column, PredicateKind.IN, values=tuple(values))

    @staticmethod
    def is_null(column):
        return Predicate(column, PredicateKind.IS_NULL)

    @staticmethod
    def not_null(column):
        return Predicate(column, PredicateKind.NOT_NULL)


# ---------------------------------------------------------------- 文件统计

@dataclass
class ColumnStats:
    """单文件单列统计。

    字符串截断标志（见需求"统计缺失或可能截断时保留文件"）：
    - present=False：统计缺失（None），该列上的任何数据谓词都不能裁剪；
    - truncated=True：min/max 可能不是真实极值（被截断），相关方向判 INCONCLUSIVE；
    - null_count 为 None 时表示未知，NULL 谓词保守不裁剪。
    """
    column: str
    type: str                          # "int64" | "double" | "string" | "timestamp" ...
    min_value: Any = None
    max_value: Any = None
    null_count: Optional[int] = None
    row_count: int = 0
    present: bool = True
    truncated: bool = False
    truncation_note: str = ""

    @property
    def min_present(self) -> bool:
        return self.present and self.min_value is not None

    @property
    def max_present(self) -> bool:
        return self.present and self.max_value is not None


@dataclass
class FileEntry:
    file_id: str
    physical_path: str
    partition_value: Optional[str]     # 该文件所属分区桶值，如 "2024-02"
    row_count: int
    stats: dict[str, ColumnStats] = field(default_factory=dict)
    size_bytes: int = 0


@dataclass
class PartitionEntry:
    column: str
    value: str                         # 桶值，如 "2024-02"
    files: list[FileEntry] = field(default_factory=list)


@dataclass
class TableMetadata:
    table: str
    partition_column: str
    transform: dict
    partitions: list[PartitionEntry] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)  # 列名 -> 逻辑类型

    def iter_files(self):
        for p in self.partitions:
            for f in p.files:
                yield p, f


# ---------------------------------------------------------------- 裁剪决策

class Layer(str, enum.Enum):
    PARTITION = "partition"
    FILE_STATS = "file_stats"
    KEPT = "kept"


class PruneReason(str, enum.Enum):
    # 分区层
    PARTITION_OUTSIDE_RANGE = "partition_outside_range"
    PARTITION_NO_MATCH_IN_LIST = "partition_no_match_in_list"
    PARTITION_NULL_IMPOSSIBLE = "partition_null_impossible"
    PARTITION_KEY_TRANSFORM_MISMATCH = "partition_key_transform_mismatch"
    # 文件统计层
    STATS_BELOW_LOWER = "stats_below_lower"
    STATS_ABOVE_UPPER = "stats_above_upper"
    STATS_EQ_NO_OVERLAP = "stats_eq_no_overlap"
    STATS_IN_NO_OVERLAP = "stats_in_no_overlap"
    STATS_ALL_NULL_VS_NOT_NULL = "stats_all_null_vs_not_null"
    STATS_NO_NULL_VS_IS_NULL = "stats_no_null_vs_is_null"
    # 未裁剪（保留）
    KEPT_BY_PREDICATE = "kept_by_predicate"
    KEPT_STATS_MISSING = "kept_stats_missing"
    KEPT_STATS_TRUNCATED = "kept_stats_truncated"
    KEPT_NULL_COUNT_UNKNOWN = "kept_null_count_unknown"
    KEPT_PARTITION_OVERLAPS = "kept_partition_overlaps"
    KEPT_NO_PREDICATE = "kept_no_predicate"


class Certainty(str, enum.Enum):
    PRUNED = "pruned"            # 有确定性证据，该文件不可能匹配
    KEPT = "kept"                # 必须扫描（可能匹配 / 证据不足）


@dataclass
class Decision:
    target_type: str             # "partition" | "file"
    target_id: str
    layer: Layer
    certainty: Certainty
    reason: PruneReason
    detail: str
    predicate_column: Optional[str] = None
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "layer": self.layer.value,
            "certainty": self.certainty.value,
            "reason": self.reason.value,
            "detail": self.detail,
            "predicate_column": self.predicate_column,
            "evidence": self.evidence,
        }


def model_to_dict(obj) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, (list, tuple)):
        return [model_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: model_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, enum.Enum):
        return obj.value
    return asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj


@dataclass
class PrunePlan:
    request_id: str
    table: str
    predicates: list[Predicate]
    decisions: list[Decision] = field(default_factory=list)
    selected_files: list[str] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    kernel_version: str = ""
    transform_version: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "table": self.table,
            "kernel_version": self.kernel_version,
            "transform_version": self.transform_version,
            "predicates": [
                {"column": p.column, "kind": p.kind.value,
                 "value": p.value, "values": list(p.values) if p.values else None,
                 "lower": p.lower, "upper": p.upper,
                 "lower_inclusive": p.lower_inclusive,
                 "upper_inclusive": p.upper_inclusive}
                for p in self.predicates],
            "selected_files": self.selected_files,
            "totals": self.totals,
            "decisions": [d.to_dict() for d in self.decisions],
            "uncertain_notes": self.notes,
        }
