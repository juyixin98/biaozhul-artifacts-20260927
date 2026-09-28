"""按 Parquet 逻辑类型做比较 —— 浮点 NaN 与有符号零单独定义。

Parquet 的 min/max 统计遵循 parquet-stats.md 规定的 *total order*：
- BOOLEAN / INT32 / INT64：数值序；
- FLOAT / DOUBLE：在数值序基础上规定 NaN 排在所有有限值与无穷之后，
  且 -NaN < +NaN；-0.0 与 +0.0 是 *不同* 的边界（-0.0 在前）；
- BYTE_ARRAY / FIXED_LEN_BYTE_ARRAY：无符号字节字典序。

审计重算真值与检查聚合关系时全部使用本模块的 key/相等定义，不直接
使用 Python 的 ``min``/``==``（它们把 NaN 当无序、+/-0.0 当相等）。
"""
from __future__ import annotations

import math
import struct
from typing import Any, Iterable

NAN_PLUS = math.nan
NAN_MINUS = -math.nan


def _float_bits(value: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", value))[0]


def float32_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def float_total_key(value: float) -> int:
    """DOUBLE total order 的排序键（升序即 Parquet total order）。

    顺序（从小到大）：-Inf … 负有限数（绝对值大的在前）… -0.0 … +0.0 …
    正有限数 … +Inf … -NaN（符号位为 1 的 NaN）… +NaN。

    实现采用 IEEE-754 totalOrder 的经典整数映射：负数整体翻转 64 位，
    正数只翻符号位；NaN 提升到无穷之上并按符号区分。
    """
    bits = _float_bits(value)
    if math.isnan(value):
        # NaN 排在所有非 NaN 之后；-NaN < +NaN
        return (1 << 65) | (0 if bits >> 63 else 1)
    if bits >> 63:
        # 负数（含 -0.0、-Inf）：翻转全部位 -> 绝对值越大键越小
        return 0xFFFFFFFFFFFFFFFF ^ bits
    # 正数（含 +0.0、+Inf）：只翻符号位
    return (1 << 63) | bits


def float32_total_key(value: float) -> int:
    """FLOAT（32 位）total order 键。"""
    bits = float32_bits(value)
    if math.isnan(value):
        return (1 << 33) | (0 if bits >> 31 else 1)
    if bits >> 31:
        return 0xFFFFFFFF ^ bits
    return (1 << 31) | bits


def is_negative_zero(value: Any) -> bool:
    return isinstance(value, float) and value == 0.0 and math.copysign(1.0, value) < 0


def is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def order_key(value: Any, physical_type: str) -> Any:
    """返回某物理类型值的 total-order 比较键。"""
    if physical_type in ("DOUBLE", "FLOAT"):
        return float_total_key(value) if physical_type == "DOUBLE" else float32_total_key(value)
    if physical_type in ("BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"):
        return as_bytes(value)
    # BOOLEAN / INT32 / INT64
    return value


def as_bytes(value: Any) -> bytes:
    """把 str/bytes/bytearray 统一为字节（字符串按 UTF-8）。"""
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)


def values_equal(a: Any, b: Any, physical_type: str) -> bool:
    """逻辑相等：NaN 区分符号；-0.0 != +0.0。"""
    if physical_type in ("DOUBLE", "FLOAT"):
        if is_nan(a) and is_nan(b):
            keyfn = float_total_key if physical_type == "DOUBLE" else float32_total_key
            return keyfn(a) == keyfn(b)
        if a == 0.0 and b == 0.0:
            return is_negative_zero(a) == is_negative_zero(b)
        return a == b and not (is_nan(a) or is_nan(b))
    if physical_type in ("BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"):
        norm = lambda v: v.encode("utf-8") if isinstance(v, str) else bytes(v)
        return norm(a) == norm(b)
    return a == b


def cmp_key(key_a: Any, key_b: Any) -> int:
    if key_a < key_b:
        return -1
    if key_a > key_b:
        return 1
    return 0


def total_min(values: Iterable[Any], physical_type: str) -> Any:
    it = iter(values)
    best = next(it)
    best_key = order_key(best, physical_type)
    for v in it:
        k = order_key(v, physical_type)
        if k < best_key:
            best, best_key = v, k
    return best


def total_max(values: Iterable[Any], physical_type: str) -> Any:
    it = iter(values)
    best = next(it)
    best_key = order_key(best, physical_type)
    for v in it:
        k = order_key(v, physical_type)
        if k > best_key:
            best, best_key = v, k
    return best
