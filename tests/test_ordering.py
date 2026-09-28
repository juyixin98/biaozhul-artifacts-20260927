"""规则 1：按逻辑类型比较 —— 浮点 NaN 与有符号零单独定义。"""
from __future__ import annotations

import math
import struct

from colstats.ordering import (
    as_bytes,
    float32_total_key,
    float_total_key,
    is_negative_zero,
    total_max,
    total_min,
    values_equal,
)


def test_total_order_full_sequence():
    seq = [
        float("nan"), 1.0, -float("nan"), -0.0, 0.0,
        -2.0, float("inf"), float("-inf"),
    ]
    ordered = sorted(seq, key=float_total_key)
    labels = []
    for v in ordered:
        if math.isnan(v):
            labels.append("NaN-" if struct.unpack("<Q", struct.pack("<d", v))[0] >> 63 else "NaN+")
        elif v == 0.0:
            labels.append("-0" if is_negative_zero(v) else "+0")
        else:
            labels.append(v)
    assert labels == [
        float("-inf"), -2.0, "-0", "+0", 1.0, float("inf"), "NaN-", "NaN+",
    ]


def test_signed_zero_distinguished():
    assert float_total_key(-0.0) < float_total_key(0.0)
    assert values_equal(-0.0, -0.0, "DOUBLE")
    assert not values_equal(-0.0, 0.0, "DOUBLE")
    f32_neg = struct.unpack("<f", struct.pack("<f", -0.0))[0]
    assert float32_total_key(f32_neg) < float32_total_key(0.0)


def test_nan_sign_distinguished():
    assert values_equal(math.nan, math.nan, "DOUBLE")
    assert not values_equal(math.nan, -math.nan, "DOUBLE")
    assert float_total_key(-math.nan) < float_total_key(math.nan)
    # NaN 严格在 +Inf 之后
    assert float_total_key(math.nan) > float_total_key(float("inf"))
    assert float_total_key(-math.nan) > float_total_key(float("inf"))


def test_total_min_max_with_nan_and_zero():
    data = [1.0, math.nan, -0.0, 0.0, -2.0]
    mn = total_min(data, "DOUBLE")
    assert mn == -2.0
    # 含 -0.0 的集合，min 不应错误地给 +0.0
    assert total_min([0.0, -0.0], "DOUBLE") == -0.0
    assert is_negative_zero(total_min([0.0, -0.0], "DOUBLE"))
    assert total_max([-0.0, 0.0], "DOUBLE") == 0.0
    assert not is_negative_zero(total_max([-0.0, 0.0], "DOUBLE"))
    # 全 NaN 时 total_max 由 total order 给 +NaN
    assert math.isnan(total_max([math.nan, -math.nan], "DOUBLE"))


def test_bytes_ordering_utf8():
    assert values_equal(b"abc", "abc", "BYTE_ARRAY")
    assert as_bytes("abc") == b"abc"
    assert values_equal(b"a", b"a", "BYTE_ARRAY")
    assert not values_equal(b"a", b"b", "BYTE_ARRAY")
    # 无符号字节序：0x80 > 0x7f（不同于有符号 char）
    assert b"\x7f" < as_bytes(b"\x80")


def test_integer_and_bool_equality():
    assert values_equal(3, 3, "INT32")
    assert not values_equal(3, 4, "INT32")
    assert values_equal(True, True, "BOOLEAN")
    assert not values_equal(True, False, "BOOLEAN")
