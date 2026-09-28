"""敏感数据脱敏: 诊断输出中只暴露结构信息, 不泄露真实业务值。"""
from __future__ import annotations

import math
from typing import Any

REDACTED = "[REDACTED]"

#: 统计对象上允许原样输出的结构字段
STRUCTURAL_KEYS = {
    "logical_type",
    "count",
    "null_count",
    "nan_count",
    "has_positive_zero",
    "has_negative_zero",
    "min_truncated",
    "max_truncated",
    "sorted",
}


def mask_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return "NaN"  # NaN 不含信息, 但显式标注便于诊断
    return REDACTED


def mask_stats_dict(stats: dict[str, Any], sensitive: bool) -> dict[str, Any]:
    """统计转 JSON 后的脱敏: 敏感列只保留结构字段, min/max 打码。"""
    if not sensitive:
        return dict(stats)
    out = {k: stats.get(k) for k in STRUCTURAL_KEYS if k in stats}
    out["min"] = None if stats.get("min") is None else REDACTED
    out["max"] = None if stats.get("max") is None else REDACTED
    return out


def mask_row(row: dict[str, Any], sensitive_columns: set[str] | tuple[str, ...]):
    sensitive = set(sensitive_columns)
    return {k: (REDACTED if k in sensitive and v is not None else v)
            for k, v in row.items()}
