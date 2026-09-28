"""泛化层级校验：可验证、恒等层、包含关系、NULL 自动传播。"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.errors import FailureCode, ServiceError
from app.core.hierarchies import build_hierarchy
from app.models import NULL_VALUE

SETTINGS = Settings(allow_ephemeral_key=True)


def _lev0(values):
    return {v: v for v in values}


def test_level0_identity_required():
    with pytest.raises(ServiceError) as ei:
        build_hierarchy(
            "c",
            [{"a": "a", "b": "WRONG"}],
            {"a", "b"},
            SETTINGS,
        )
    assert ei.value.code is FailureCode.INVALID_HIERARCHY


def test_level0_must_cover_observed_values():
    with pytest.raises(ServiceError) as ei:
        build_hierarchy("c", [{"a": "a"}], {"a", "b"}, SETTINGS)
    assert ei.value.code is FailureCode.HIERARCHY_NOT_COVERING
    assert ei.value.details["missing_values_count"] == 1


def test_non_monotone_split_is_rejected():
    # 下层 {a,b}, {c,d}；上层把 {a,b} 拆开与 c/d 重组 → 违反包含关系
    levels = [
        _lev0(["a", "b", "c", "d"]),
        {"a": "G1", "b": "G1", "c": "G2", "d": "G2"},
        {"a": "X", "b": "Y", "c": "X", "d": "Y"},  # 交叉重排
    ]
    with pytest.raises(ServiceError) as ei:
        build_hierarchy("c", levels, {"a", "b", "c", "d"}, SETTINGS)
    assert ei.value.code is FailureCode.HIERARCHY_NOT_MONOTONE


def test_non_monotone_crossing_is_rejected():
    # 上层组 {a,c} 跨越下层边界 {a,b}
    levels = [
        _lev0(["a", "b", "c"]),
        {"a": "G1", "b": "G1", "c": "G2"},
        {"a": "X", "b": "Y", "c": "X"},
    ]
    with pytest.raises(ServiceError) as ei:
        build_hierarchy("c", levels, {"a", "b", "c"}, SETTINGS)
    assert ei.value.code is FailureCode.HIERARCHY_NOT_MONOTONE


def test_valid_merging_hierarchy_is_built():
    levels = [
        _lev0(["a", "b", "c", "d"]),
        {"a": "G1", "b": "G1", "c": "G2", "d": "G2"},
        {"a": "ALL", "b": "ALL", "c": "ALL", "d": "ALL"},
    ]
    h = build_hierarchy("c", levels, {"a", "b", "c", "d"}, SETTINGS)
    assert h.height == 2
    assert h.group_size("a", 1) == 2
    assert h.group_size("a", 2) == 4
    # 单调性的直接结构断言：上层组是下层组的并集
    assert h.groups[1]["G1"] <= h.groups[2]["ALL"]


def test_null_is_auto_propagated_and_never_merged():
    levels = [
        {"a": "a", "b": "b"},
        {"a": "G", "b": "G"},
    ]
    h = build_hierarchy("c", levels, {"a", "b", NULL_VALUE}, SETTINGS)
    assert h.apply(NULL_VALUE, 1) == NULL_VALUE
    assert h.group_size(NULL_VALUE, 1) == 1  # NULL 永远独立成组


def test_declaring_null_in_hierarchy_is_rejected():
    with pytest.raises(ServiceError) as ei:
        build_hierarchy(
            "c",
            [{NULL_VALUE: NULL_VALUE, "a": "a"}, {NULL_VALUE: "x", "a": "a"}],
            {"a", NULL_VALUE},
            SETTINGS,
        )
    assert ei.value.code is FailureCode.INVALID_HIERARCHY


def test_levels_must_cover_same_keyset():
    levels = [
        _lev0(["a", "b", "c"]),
        {"a": "X", "b": "X"},  # 缺 c
    ]
    with pytest.raises(ServiceError) as ei:
        build_hierarchy("c", levels, {"a", "b", "c"}, SETTINGS)
    assert ei.value.code is FailureCode.HIERARCHY_NOT_COVERING


def test_too_deep_hierarchy_rejected():
    s = Settings(allow_ephemeral_key=True, max_hierarchy_height=1)
    levels = [_lev0(["a"]), {"a": "x"}, {"a": "y"}]
    with pytest.raises(ServiceError) as ei:
        build_hierarchy("c", levels, {"a"}, s)
    assert ei.value.code is FailureCode.HIERARCHY_TOO_DEEP
