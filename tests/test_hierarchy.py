"""泛化层级测试：包含关系、覆盖完整性、prefix 严格递减、NULL 跨级保留。"""

from __future__ import annotations

import pytest

from anon_risk.errors import ErrorCode
from anon_risk.kernel.hierarchy import apply_vector, materialize
from anon_risk.kernel.parser import parse_dataset
from anon_risk.kernel.types import NULL


def _dataset(hierarchies, rows):
    payload = {
        "columns": ["z", "s"],
        "quasi_identifiers": ["z"],
        "sensitive": ["s"],
        "rows": rows,
        "hierarchies": {"z": {"levels": hierarchies}},
    }
    return parse_dataset(payload)


def test_map_must_cover_domain():
    ds = _dataset(
        [{"rule": "map", "mapping": {"a": "x"}}],
        [["a", "S"], ["b", "T"]],
    )
    with pytest.raises(Exception) as ei:
        materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert ei.value.code is ErrorCode.HIERARCHY_INCOMPLETE
    assert ei.value.details["missing_key_count"] == 1  # b 未被覆盖


def test_map_unknown_key_is_error_not_silent_ignore():
    ds = _dataset(
        [{"rule": "map", "mapping": {"a": "x", "b": "y", "ghost": "g"}}],
        [["a", "S"], ["b", "T"]],
    )
    with pytest.raises(Exception) as ei:
        materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert ei.value.code is ErrorCode.HIERARCHY_UNKNOWN_KEY
    assert ei.value.details["unknown_key_count"] == 1


def test_chained_map_containment_holds_and_merges():
    ds = _dataset(
        [
            {"rule": "map", "mapping": {"a": "ab", "b": "ab", "c": "cd", "d": "cd"}},
            {"rule": "map", "mapping": {"ab": "ALL", "cd": "ALL"}},
        ],
        [["a", "S"], ["b", "T"], ["c", "U"], ["d", "V"]],
    )
    mh = materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    keys = apply_vector(ds.rows, ["z"], {"z": mh}, {"z": 2})
    assert set(keys) == {("ALL",)}  # 四级合并为一 => 包含关系成立


def test_chained_map_split_detected_via_construction():
    # 第二级 mapping 里同一上一级标签出现两个目标键在语法上不可能（dict），
    # 但“上一级标签未覆盖”必须被识别为不完整
    ds = _dataset(
        [
            {"rule": "map", "mapping": {"a": "ab", "b": "ab"}},
            {"rule": "map", "mapping": {"ab": "ALL"}},
        ],
        [["a", "S"], ["b", "T"], ["a", "U"]],
    )
    mh = materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert mh.validation["levels"][1]["distinct_labels"] == 1


def test_prefix_keep_must_strictly_decrease():
    ds = _dataset(
        [{"rule": "prefix", "keep": 3}, {"rule": "prefix", "keep": 3}],
        [["abcd", "S"], ["abce", "T"]],
    )
    with pytest.raises(Exception) as ei:
        materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert ei.value.code is ErrorCode.HIERARCHY_KEEP_NOT_DECREASING


def test_prefix_incomplete_when_value_too_short():
    ds = _dataset(
        [{"rule": "prefix", "keep": 5}],
        [["abcd", "S"], ["abcdef", "T"]],
    )
    with pytest.raises(Exception) as ei:
        materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert ei.value.code is ErrorCode.HIERARCHY_INCOMPLETE
    assert ei.value.details["short_value_count"] == 1


def test_range_bins_must_be_coarser_at_each_level():
    # 第一级 10 宽、第二级变 5 宽 => 第二级把第一级的箱子拆开 => 必须报不包含
    ds = _dataset(
        [
            {"rule": "range", "bins": [0, 10, 20], "labels": ["A", "B"]},
            {"rule": "range", "bins": [0, 5, 10, 15, 20],
             "labels": ["a1", "a2", "b1", "b2"]},
        ],
        [["2", "S"], ["7", "T"], ["12", "U"], ["17", "V"]],
    )
    with pytest.raises(Exception) as ei:
        materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert ei.value.code is ErrorCode.HIERARCHY_NOT_CONTAINING


def test_range_out_of_bounds_is_incomplete():
    ds = _dataset(
        [{"rule": "range", "bins": [0, 10], "labels": ["A"]}],
        [["5", "S"], ["50", "T"]],
    )
    with pytest.raises(Exception) as ei:
        materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert ei.value.code is ErrorCode.HIERARCHY_INCOMPLETE
    assert ei.value.details["out_of_range_count"] == 1


def test_range_non_numeric_is_incomplete():
    ds = _dataset(
        [{"rule": "range", "bins": [0, 10], "labels": ["A"]}],
        [["5", "S"], ["abc", "T"]],
    )
    with pytest.raises(Exception) as ei:
        materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert ei.value.code is ErrorCode.HIERARCHY_INCOMPLETE
    assert ei.value.details["non_numeric_count"] == 1


def test_null_preserved_through_all_levels():
    ds = _dataset(
        [
            {"rule": "map", "mapping": {"a": "ab", "b": "ab"}},
            {"rule": "map", "mapping": {"ab": "ALL"}},
        ],
        [[None, "S"], ["a", "T"], ["b", "U"]],
    )
    mh = materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert mh.validation["null_present"] is True
    keys = apply_vector(ds.rows, ["z"], {"z": mh}, {"z": 2})
    assert keys[0] == (NULL,)          # NULL 没有被映射成 ALL
    assert keys[1] == keys[2] == ("ALL",)
    # NULL 行自成类但仍在样本内
    assert len(keys) == 3


def test_general_star_label_accepted():
    ds = _dataset(
        [{"rule": "map", "mapping": {"a": "*", "b": "*"}}],
        [["a", "S"], ["b", "T"]],
    )
    mh = materialize(ds.hierarchies["z"], [r["z"] for r in ds.rows])
    assert mh.validation["levels"][0]["contains_general"] is True
