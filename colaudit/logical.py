"""逻辑类型与比较语义 (验收规则 1: 按逻辑类型比较)。

特殊语义:
* 浮点 NaN: 排序时 NaN 大于一切非 NaN 值; NaN 不参与 SQL 式等值/比较
  (``nan_equal`` 为 False, ``compare`` 六个谓词对 NaN 均为 False)。
* 有符号零: -0.0 与 +0.0 作为"值"相等 (``value_equal`` 为 True),
  但作为 min/max 端点是两个不同的端点 (``endpoint_equal`` 为 False),
  并在列统计中分别用 ``has_positive_zero`` / ``has_negative_zero`` 记录。
* 日期按日历序数比较, 不与数字混类; 布尔按 False < True; 字符串按码点比较。
"""
from __future__ import annotations

import datetime as _dt
import math
from enum import Enum

import pyarrow as pa


class LogicalType(str, Enum):
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    DATE = "date"


def from_arrow(arrow_type: pa.DataType) -> LogicalType:
    """把 Arrow 物理类型映射到审计使用的逻辑类型。"""
    if pa.types.is_integer(arrow_type):
        return LogicalType.INT
    if pa.types.is_floating(arrow_type):
        return LogicalType.FLOAT
    if pa.types.is_boolean(arrow_type):
        return LogicalType.BOOL
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return LogicalType.STRING
    if pa.types.is_date(arrow_type):
        return LogicalType.DATE
    raise TypeError(f"不支持的 Arrow 类型: {arrow_type}")


def classify(arrow_type: pa.DataType) -> LogicalType:
    return from_arrow(arrow_type)


def is_nan(value: object) -> bool:
    return isinstance(value, float) and math.isnan(value)


def is_signed_zero(value: object) -> bool:
    return isinstance(value, float) and value == 0.0


def zero_sign(value: float) -> str:
    """``+`` 表示 +0.0, ``-`` 表示 -0.0; 非零浮点数返回空串。"""
    if not is_signed_zero(value):
        return ""
    return "+" if math.copysign(1.0, value) > 0 else "-"


def date_ordinal(value: object) -> int:
    if isinstance(value, _dt.date):
        return value.toordinal()
    raise TypeError(f"期望 date, 实际 {type(value).__name__}")


def sort_key(value: object, logical: LogicalType) -> tuple:
    """生成可直接用 Python ``<`` 比较的排序键。

    返回 (nan_bucket, order_value): 非 NaN 落在桶 0, NaN 落在桶 1,
    因此所有 NaN 都排在非 NaN 之后; 同桶内按 order_value 比较。
    -0.0 与 +0.0 的 order_value 相同 (排序序位一致), 端点差异另行区分。
    """
    if value is None:
        raise ValueError("NULL 不参与排序键")
    if logical is LogicalType.FLOAT:
        if is_nan(value):
            return (1, 0)
        # +0.0/-0.0 -> 0.0, 排序序位相同
        return (0, value + 0.0)
    if logical is LogicalType.DATE:
        return (0, date_ordinal(value))
    if logical is LogicalType.BOOL:
        return (0, 1 if value else 0)
    # int / string: 原生值即可比较
    return (0, value)


def cmp(left: object, right: object, logical: LogicalType) -> int:
    """逻辑类型意义下的三态比较; NaN 不与任何值 (含自身) 比较。

    :raises TypeError: 任一侧为 NaN。
    """
    if is_nan(left) or is_nan(right):
        raise TypeError("NaN 不可比较")
    lk = sort_key(left, logical)
    rk = sort_key(right, logical)
    if lk < rk:
        return -1
    if lk > rk:
        return 1
    return 0


def value_equal(left: object, right: object, logical: LogicalType) -> bool:
    """SQL 式值相等: NaN 不等于任何值 (含自身); -0.0 == +0.0。"""
    if is_nan(left) or is_nan(right):
        return False
    try:
        return cmp(left, right, logical) == 0
    except TypeError:  # pragma: no cover - NaN 已在上面拦截
        return False


def endpoint_equal(left: object, right: object, logical: LogicalType) -> bool:
    """min/max 端点相等: 浮点区分 -0.0 与 +0.0, NaN 只与 NaN 相等。

    端点用于描述"列中真实出现过的那个值", 因此有符号零必须可区分。
    """
    if logical is LogicalType.FLOAT:
        if is_nan(left) and is_nan(right):
            return True
        if is_nan(left) or is_nan(right):
            return False
        if is_signed_zero(left) and is_signed_zero(right):
            return zero_sign(left) == zero_sign(right)
        return value_equal(left, right, logical)
    return value_equal(left, right, logical)


# SQL 谓词集合 (IS NULL / IS NOT NULL 不经过 compare)
PREDICATES = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})


def compare(op: str, value: object, target: object, logical: LogicalType) -> bool:
    """对单个非 NULL 值求值六元比较谓词。

    任一侧为 NaN 时六个谓词一律 False (三值逻辑中比较结果为 UNKNOWN,
    行级判定按"不匹配"处理, 与 IS NULL 语义分离)。
    """
    if op not in PREDICATES:
        raise ValueError(f"未知谓词: {op}")
    if is_nan(value) or is_nan(target):
        return False
    c = cmp(value, target, logical)
    return {
        "eq": c == 0,
        "ne": c != 0,
        "lt": c < 0,
        "le": c <= 0,
        "gt": c > 0,
        "ge": c >= 0,
    }[op]
