"""审计领域的共享数据模型。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal

# 审计结论
Verdict = Literal["ACCEPTED", "REJECTED", "UNDECIDABLE"]


class Severity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class Claim:
    """一份 min/max/NULL 统计声明（页级、块级或行组级共用）。

    - ``min_claim``/``max_claim`` 是按 *物理字节解码后* 的 Python 值；
    - ``has_min_max`` 为 False 表示文件里根本没有 min/max 统计；
    - ``min_truncated``/``max_truncated`` 对应 Parquet 的截断标志，
      影响可信范围（规则 2）。
    """

    has_min_max: bool
    null_count: int | None = None
    has_null_count: bool = False
    min_claim: Any = None
    max_claim: Any = None
    min_truncated: bool = False
    max_truncated: bool = False
    num_values: int = 0  # 页头中的 num_values（含 NULL）

    @property
    def exact(self) -> bool:
        """是否可按"精确边界"信任（无截断）。"""
        return self.has_min_max and not self.min_truncated and not self.max_truncated


@dataclass
class PageClaim:
    page_index: int
    offset: int
    header_offset: int
    num_values: int
    claim: Claim
    raw_header: list = field(repr=False, default=None)  # type: ignore[assignment]
    compressed_size: int = 0       # 到下一页页头的物理跨度（含 CRC）
    data_size: int = 0             # 页头声明的数据大小（不含 CRC）


@dataclass
class ChunkClaim:
    column_index: int
    path: str
    physical_type: str
    logical_type: str | None
    converted_type: str | None
    data_offset: int
    total_compressed_size: int
    num_values: int
    claim: Claim
    pages: list[PageClaim] = field(default_factory=list)


@dataclass
class SortingClaim:
    column_idx: int
    descending: bool
    nulls_first: bool


@dataclass
class RowGroupClaim:
    row_group_index: int
    num_rows: int
    chunks: list[ChunkClaim] = field(default_factory=list)
    sorting: list[SortingClaim] = field(default_factory=list)


@dataclass
class ColumnSchema:
    index: int
    path: str
    physical_type: str
    logical_type: str | None
    converted_type: str | None
    max_repetition: int
    max_definition: int


@dataclass
class FileModel:
    """格式适配层对一个 Parquet 文件的完整解析结果。"""

    path: str
    size: int
    num_rows: int
    schema: list[ColumnSchema]
    row_groups: list[RowGroupClaim]
    # 原始字节与页脚节点，供改写使用
    raw: bytes = field(repr=False, default=b"")
    footer_nodes: list = field(repr=False, default=None)  # type: ignore[assignment]


@dataclass
class GroundTruth:
    """从实际数据独立重算出来的真值。"""

    num_values: int
    null_count: int
    has_values: bool            # 是否存在任何非 NULL 值
    min_value: Any = None
    max_value: Any = None
    contains_nan: bool = False
    contains_negative_zero: bool = False
    values_sorted_asc: bool = False
    values_sorted_desc: bool = False


@dataclass
class Finding:
    """单条审计发现。可序列化为 JSON 存入 SQLite。"""

    code: str                  # 失败类别，例如 NULL_COUNT_MISMATCH
    severity: Severity
    locator: dict              # row_group/column/page 定位
    message: str               # 人可读说明（含接受/拒绝原因）
    expected: dict | None = None
    observed: dict | None = None
    request_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "locator": self.locator,
            "message": self.message,
            "expected": self.expected,
            "observed": self.observed,
            "request_id": self.request_id,
        }


@dataclass
class AuditResult:
    path: str
    verdict: Verdict
    audit_id: str | None
    findings: list[Finding]
    # 每个列位置 -> 是否可安全剪枝（False 时查询层必须全扫）
    trusted: dict[str, bool]
    truncated_columns: list[str]
    summary: dict
