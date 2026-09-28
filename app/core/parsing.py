"""解析与规则校验层。

职责：把 API 输入（原始 dict 行）解析为分析内核使用的规整结构，
所有拒绝都有明确证据（错误码 + 列名/行号/取值类别，绝不回显多余原始数据）。

关键规则
========
* 列角色必须显式声明；至少一个准标识符(QI)、一个敏感字段；
* QI 必须带泛化层级；
* 行内未知列 → 拒绝；缺失列 → 视为 NULL 并计入 ``null_rows``（不丢样本）；
* 重复列名 / 重复行 id → 拒绝；
* NULL 规范化为哨兵保留；
* 尺寸上限在此强制。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.core.errors import FailureCode, ServiceError
from app.core.hierarchies import Hierarchy, build_hierarchy
from app.models import NULL_VALUE, ColumnRole, DatasetIn, canonical


@dataclass(frozen=True)
class Column:
    name: str
    role: ColumnRole
    hierarchy: Hierarchy | None = None


@dataclass
class Dataset:
    name: str
    columns: list[Column]
    # rows: 每行是 {列名: 规范化字符串}
    rows: list[dict[str, str]]
    row_ids: list[str]
    k: int
    l: int
    fail_on_unreachable: bool
    null_row_flags: list[bool] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # --- 便捷访问器 ---
    @property
    def qi_columns(self) -> list[Column]:
        return [c for c in self.columns if c.role is ColumnRole.QUASI_IDENTIFIER]

    @property
    def sensitive_columns(self) -> list[Column]:
        return [c for c in self.columns if c.role is ColumnRole.SENSITIVE]

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise ServiceError(FailureCode.UNKNOWN_COLUMN, f"column '{name}' not found")


def parse_dataset(payload: DatasetIn, settings: Settings) -> Dataset:
    """把经过 Pydantic 类型校验的提交进一步做语义/规则校验并规范化。"""
    warnings: list[str] = []

    # --- 列声明 ---
    if len(payload.columns) > settings.max_columns:
        raise ServiceError(
            FailureCode.COLUMN_LIMIT_EXCEEDED,
            f"too many columns: {len(payload.columns)} > {settings.max_columns}",
            {"got": len(payload.columns), "limit": settings.max_columns},
        )

    names = [c.name for c in payload.columns]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ServiceError(
            FailureCode.DUPLICATE_COLUMN,
            "duplicate column names",
            {"columns": dupes},
        )

    qi = [c for c in payload.columns if c.role is ColumnRole.QUASI_IDENTIFIER]
    sens = [c for c in payload.columns if c.role is ColumnRole.SENSITIVE]
    if not qi:
        raise ServiceError(
            FailureCode.NO_QUASI_IDENTIFIER,
            "at least one column must be explicitly declared quasi_identifier",
        )
    if not sens:
        raise ServiceError(
            FailureCode.NO_SENSITIVE,
            "at least one column must be explicitly declared sensitive",
        )
    if len(qi) > settings.max_qi:
        raise ServiceError(
            FailureCode.COLUMN_LIMIT_EXCEEDED,
            f"too many quasi_identifiers: {len(qi)} > {settings.max_qi}",
            {"got": len(qi), "limit": settings.max_qi},
        )

    if len(payload.rows) > settings.max_rows:
        raise ServiceError(
            FailureCode.ROW_LIMIT_EXCEEDED,
            f"too many rows: {len(payload.rows)} > {settings.max_rows}",
            {"got": len(payload.rows), "limit": settings.max_rows},
        )
    if not payload.rows:
        raise ServiceError(
            FailureCode.EMPTY_DATASET,
            "dataset contains zero rows; risk analysis requires a non-empty sample",
        )

    # --- 构建层级（含包含关系强校验）---
    columns: list[Column] = []
    raw_values_by_qi: dict[str, set[str]] = {c.name: set() for c in qi}

    declared = set(names)
    normalized_rows: list[dict[str, str]] = []
    row_ids: list[str] = []
    null_row_flags: list[bool] = []
    missing_counts: dict[str, int] = {n: 0 for n in names}
    unknown_seen: set[str] = set()

    for idx, raw in enumerate(payload.rows):
        if not isinstance(raw, dict):
            raise ServiceError(
                FailureCode.INVALID_INPUT,
                f"row {idx} must be an object mapping column names to values",
                {"row_index": idx},
            )
        extra = set(raw) - declared
        if extra:
            unknown_seen.update(extra)
        row: dict[str, str] = {}
        row_has_null_qi = False
        for col in payload.columns:
            if col.name in raw:
                val = canonical(raw[col.name])
            else:
                # 缺失键 = NULL，但不丢样本
                val = NULL_VALUE
                missing_counts[col.name] += 1
            row[col.name] = val
            if col.role is ColumnRole.QUASI_IDENTIFIER:
                raw_values_by_qi[col.name].add(val)
                if val == NULL_VALUE:
                    row_has_null_qi = True
        normalized_rows.append(row)
        null_row_flags.append(row_has_null_qi)

    if unknown_seen:
        raise ServiceError(
            FailureCode.UNKNOWN_COLUMN,
            "row contains keys not declared in columns",
            {"unknown_keys": sorted(unknown_seen)},
        )

    # 行 id：调用方可显式提供（通过保留键不现实），这里用稳定序号；
    # 重复行检测基于完整规范化行（重复行本身允许——它们正是等价类的成员）。
    for i in range(len(normalized_rows)):
        row_ids.append(f"row_{i:06d}")

    for spec in payload.columns:
        if spec.role is ColumnRole.QUASI_IDENTIFIER:
            assert spec.hierarchy is not None
            hier = build_hierarchy(
                spec.name,
                spec.hierarchy.levels,
                raw_values_by_qi[spec.name],
                settings,
            )
            columns.append(Column(spec.name, spec.role, hier))
        else:
            columns.append(Column(spec.name, spec.role, None))

    for n, count in missing_counts.items():
        if count:
            warnings.append(
                f"column '{n}': {count} row(s) missing the key; treated as NULL and "
                "retained in the sample (not silently dropped)"
            )

    n_null_rows = sum(null_row_flags)
    if n_null_rows:
        warnings.append(
            f"{n_null_rows} row(s) contain NULL in at least one quasi_identifier; "
            "they remain in equivalence-class counts"
        )

    return Dataset(
        name=payload.name,
        columns=columns,
        rows=normalized_rows,
        row_ids=row_ids,
        k=payload.k,
        l=payload.l,
        fail_on_unreachable=payload.fail_on_unreachable,
        null_row_flags=null_row_flags,
        warnings=warnings,
    )
