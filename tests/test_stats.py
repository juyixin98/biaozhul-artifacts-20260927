"""统计重算与页->行组聚合测试: 与独立 CSV oracle 逐字段对照。"""
import math
import sys
from pathlib import Path

import pytest

from colaudit.logical import LogicalType
from colaudit.stats import ColumnStats, aggregate_stats, compute_column_stats, stats_equal
from fixtures import oracle_stats
from oracle import load_ground_truth, pages

LT = {
    "id": LogicalType.INT,
    "score": LogicalType.FLOAT,
    "name": LogicalType.STRING,
    "active": LogicalType.BOOL,
}


def _page_and_rg(fixture_root, name):
    rows = load_ground_truth(fixture_root / name)
    per_page = list(pages(rows))
    return rows, per_page


def test_compute_matches_oracle_all_datasets(fixture_root):
    for name in ("well_formed", "mixed_nan", "all_null", "bad_statistics"):
        rows, per_page = _page_and_rg(fixture_root, name)
        for col in ("id", "score", "name", "active"):
            for rg, p, page_rows in per_page:
                actual = compute_column_stats(
                    [r[col] for r in page_rows], LT[col]
                ).to_json()
                expected = oracle_stats(page_rows, col)
                # sorted 语义: 全 NULL 页 oracle 与实现都应为 None
                assert actual["null_count"] == expected["null_count"], (
                    f"{name} rg{rg} p{p} {col} null_count")
                assert actual["nan_count"] == expected["nan_count"]
                assert actual["count"] == expected["count"]
                _assert_endpoint(actual["min"], expected["min"], LT[col],
                                 f"{name} rg{rg} p{p} {col}.min")
                _assert_endpoint(actual["max"], expected["max"], LT[col],
                                 f"{name} rg{rg} p{p} {col}.max")
                assert actual["sorted"] == expected["sorted"], (
                    f"{name} rg{rg} p{p} {col} sorted")


def _assert_endpoint(actual, expected, logical, label):
    if expected is None:
        assert actual is None, label
        return
    if logical is LogicalType.FLOAT:
        if math.isnan(expected):
            assert isinstance(actual, float) and math.isnan(actual), label
        elif expected == 0.0:
            assert actual == 0.0
            assert math.copysign(1.0, actual) == math.copysign(1.0, expected), (
                label + " 有符号零")
        else:
            assert actual == expected, label
    else:
        assert actual == expected, label


def test_aggregate_pages_equals_rowgroup_oracle(fixture_root):
    rows = load_ground_truth(fixture_root / "mixed_nan")
    for col in ("id", "score", "name", "active"):
        page_stats = [
            compute_column_stats([r[col] for r in pr], LT[col])
            for _, _, pr in pages(rows)
        ]
        # 按行组分别聚合
        for rg in range(2):
            agg = aggregate_stats(page_stats[rg * 3:(rg + 1) * 3])
            rg_rows = rows[rg * 60:(rg + 1) * 60]
            expected = oracle_stats(rg_rows, col)
            same, diffs = stats_equal(
                agg, ColumnStats.from_json(expected))
            assert same, f"rg{rg} {col}: {diffs}"


def test_aggregate_count_mismatch_raises():
    p1 = compute_column_stats([1, 2], LogicalType.INT)
    p2 = compute_column_stats([3], LogicalType.INT)
    agg = aggregate_stats([p1, p2])
    assert agg.count == 3
    with pytest.raises(ValueError, match="行数不一致"):
        aggregate_stats([p1, p2], count=99)
    p3 = compute_column_stats([None, 1], LogicalType.INT)
    with pytest.raises(ValueError, match="NULL 计数不一致"):
        aggregate_stats([p1, p3], null_count=0)


def test_all_null_stats():
    st = compute_column_stats([None, None], LogicalType.INT)
    assert st.all_null
    assert st.min is None and st.max is None
    assert st.sorted is None


def test_signed_zero_flags():
    st = compute_column_stats(
        [0.0, -0.0, None, float("nan"), 1.0], LogicalType.FLOAT
    )
    assert st.has_positive_zero and st.has_negative_zero
    assert st.nan_count == 1 and st.null_count == 1
    # min 端点必须是 -0.0 (与 +0.0 可区分的真实最小端点)
    assert st.min == 0.0
    assert math.copysign(1.0, st.min) < 0
    assert st.max == 1.0
