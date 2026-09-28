"""逻辑类型系统、值规范化与 NULL 键比较规则。

语义约定（README“边界语义”有对应说明）：
- 仅支持 string/long/int/double/boolean/date 六类逻辑类型。
- JSON -> Python 的转换是严格的：布尔不与 0/1 混用；数值超界一律 VALIDATION_ERROR。
- date 逻辑类型使用 ISO 'YYYY-MM-DD' 字符串承载（本地合成夹具约定）。
- 等值删除遵循 SQL NULL 语义：删除向量中键列为 NULL 不匹配任何行（含 NULL 数据行）；
  数据行键为 NULL 也不被任何等值删除命中。比较在“规范化后的 Python 值”上进行。
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any

from app.errors import ValidationError

SUPPORTED_TYPES = frozenset({"string", "long", "int", "double", "boolean", "date"})

_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def normalize_value(value: Any, logical_type: str, *, column: str) -> Any:
    """把 JSON 传入值规范化为内部 Python 值；非法值抛 VALIDATION_ERROR。"""
    if value is None:
        return None
    try:
        if logical_type == "string":
            if isinstance(value, str):
                return value
            raise _bad(column, "expected string")
        if logical_type == "boolean":
            if isinstance(value, bool):
                return value
            raise _bad(column, "expected boolean (true/false); 0/1 are not accepted")
        if logical_type in ("int", "long"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise _bad(column, f"expected integer for {logical_type}")
            lo, hi = (_INT32_MIN, _INT32_MAX) if logical_type == "int" else (_INT64_MIN, _INT64_MAX)
            if not (lo <= value <= hi):
                raise _bad(column, f"integer out of {logical_type} range")
            return value
        if logical_type == "double":
            if isinstance(value, bool):
                raise _bad(column, "expected number for double")
            if isinstance(value, int):
                value = float(value)
            if not isinstance(value, float):
                raise _bad(column, "expected number for double")
            if math.isnan(value) or math.isinf(value):
                raise _bad(column, "NaN/Infinity are not supported")
            return value
        if logical_type == "date":
            if not isinstance(value, str):
                raise _bad(column, "expected ISO date string 'YYYY-MM-DD'")
            try:
                _dt.date.fromisoformat(value)
            except ValueError:
                raise _bad(column, "invalid ISO date; expected 'YYYY-MM-DD'")
            return value
    except ValidationError:
        raise
    raise _bad(column, f"unsupported type {logical_type}")


def _bad(column: str, message: str) -> ValidationError:
    return ValidationError("TYPE_MISMATCH", f"column '{column}': {message}", {"column": column})


def column_index(schema_columns: list[dict[str, Any]]) -> dict[str, int]:
    return {c["name"]: i for i, c in enumerate(schema_columns)}


def validate_schema(columns: list[dict[str, Any]], primary_key: list[str]) -> None:
    """建表 schema 校验：列名唯一、类型受支持、主键列存在。"""
    if not columns:
        raise ValidationError("EMPTY_SCHEMA", "schema must contain at least one column")
    names: set[str] = set()
    for c in columns:
        name = c.get("name")
        ltype = c.get("type")
        if not isinstance(name, str) or not name:
            raise ValidationError("INVALID_COLUMN", "every column requires a non-empty 'name'")
        if name in names:
            raise ValidationError("DUPLICATE_COLUMN", f"duplicate column name '{name}'", {"column": name})
        names.add(name)
        if ltype not in SUPPORTED_TYPES:
            raise ValidationError(
                "UNSUPPORTED_TYPE",
                f"column '{name}' has unsupported type {ltype!r}",
                {"column": name, "type": ltype, "supported": sorted(SUPPORTED_TYPES)},
            )
    if not primary_key:
        raise ValidationError("EMPTY_PRIMARY_KEY", "primary_key must list at least one column")
    missing = [k for k in primary_key if k not in names]
    if missing:
        raise ValidationError(
            "UNKNOWN_KEY_COLUMN", "primary key references unknown columns", {"columns": missing}
        )
