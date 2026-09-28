"""逻辑类型比较语义测试 (验收规则 1)。"""
import math

import pytest

from colaudit.logical import (
    LogicalType,
    compare,
    cmp,
    endpoint_equal,
    sort_key,
    value_equal,
)

F = LogicalType.FLOAT
I = LogicalType.INT
S = LogicalType.STRING
B = LogicalType.BOOL


def test_nan_ordering_greater_than_all_floats():
    assert sort_key(float("nan"), F) > sort_key(1e300, F)
    assert sort_key(1.0, F) < sort_key(float("nan"), F)
    with pytest.raises(TypeError):
        cmp(float("nan"), float("nan"), F)


def test_nan_sql_equality_always_false():
    assert value_equal(float("nan"), float("nan"), F) is False
    assert value_equal(float("nan"), 1.0, F) is False
    for op in ("eq", "ne", "lt", "le", "gt", "ge"):
        assert compare(op, float("nan"), 1.0, F) is False
        assert compare(op, 1.0, float("nan"), F) is False
    assert value_equal(1.0, float("nan"), F) is False


def test_signed_zero_value_equal_but_endpoint_distinct():
    assert value_equal(-0.0, 0.0, F) is True
    assert cmp(-0.0, 0.0, F) == 0
    assert endpoint_equal(-0.0, 0.0, F) is False
    assert endpoint_equal(-0.0, -0.0, F) is True
    assert endpoint_equal(0.0, 0.0, F) is True


def test_bool_and_string_order():
    assert sort_key(False, B) < sort_key(True, B)
    assert cmp("a", "b", S) < 0
    with pytest.raises(TypeError):
        cmp("a", 1, LogicalType.INT)


def test_cross_type_rejected():
    with pytest.raises(TypeError):
        cmp(0, "x", LogicalType.STRING)
