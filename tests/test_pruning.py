"""规则 2、4：截断可信范围与坏统计禁用剪枝；查询结果必须正确。"""
from __future__ import annotations

import pytest

from colstats.kernel import can_prune
from colstats.models import Claim
from colstats.parquet_adapter import read_column_values
from colstats.query import run_query
from colstats.kernel import audit_file


def make_claim(mn, mx, nulls=None, nv=100, mint=False, maxt=False, mm=True):
    return Claim(
        has_min_max=mm, null_count=nulls, has_null_count=nulls is not None,
        min_claim=mn, max_claim=mx, min_truncated=mint, max_truncated=maxt,
        num_values=nv,
    )


# ---- 规则 4：不可信统计永远不能剪枝


def test_untrusted_stats_never_prune():
    claim = make_claim(10, 20)
    for pred, v in [("eq", 999), ("lt", 0), ("gt", 999), ("between", (0, 1))]:
        if pred == "between":
            d = can_prune(claim, "INT32", "between", 0, value_high=1, trusted=False)
        else:
            d = can_prune(claim, "INT32", pred, v, trusted=False)
        assert d == "UNDECIDABLE", f"{pred} 不得在不可信时剪枝"


# ---- 基本精确边界


def test_exact_bounds_prune_outside():
    claim = make_claim(10, 20)
    assert can_prune(claim, "INT32", "eq", 9, trusted=True) == "PRUNE"
    assert can_prune(claim, "INT32", "eq", 21, trusted=True) == "PRUNE"
    assert can_prune(claim, "INT32", "eq", 15, trusted=True) == "SCAN"
    assert can_prune(claim, "INT32", "eq", 10, trusted=True) == "SCAN"
    assert can_prune(claim, "INT32", "eq", 20, trusted=True) == "SCAN"


def test_null_predicates():
    c1 = make_claim(0, 10, nulls=0)
    c2 = make_claim(0, 10, nulls=5, nv=100)
    c3 = make_claim(0, 10, nulls=100, nv=100)
    assert can_prune(c1, "INT32", "is_null", trusted=True) == "PRUNE"
    assert can_prune(c2, "INT32", "is_null", trusted=True) == "SCAN"
    assert can_prune(c3, "INT32", "not_null", trusted=True) == "PRUNE"
    c4 = Claim(has_min_max=True, has_null_count=False, num_values=100,
               min_claim=0, max_claim=10)
    assert can_prune(c4, "INT32", "is_null", trusted=True) == "UNDECIDABLE"


# ---- 规则 2：截断标志影响可信范围


def test_truncated_boundary_equality_undecidable():
    # 截断下界 alph、截断上界 b
    claim = make_claim(b"alph", b"b", mint=True, maxt=True)
    # 落在截断边界上 -> 无法判定
    assert can_prune(claim, "BYTE_ARRAY", "eq", b"alph") == "UNDECIDABLE"
    assert can_prune(claim, "BYTE_ARRAY", "lt", b"alph") == "UNDECIDABLE"
    assert can_prune(claim, "BYTE_ARRAY", "gt", b"b") == "UNDECIDABLE"
    # 严格外侧仍可剪
    assert can_prune(claim, "BYTE_ARRAY", "eq", b"aaaa") == "PRUNE"
    assert can_prune(claim, "BYTE_ARRAY", "lt", b"afff") == "PRUNE"
    assert can_prune(claim, "BYTE_ARRAY", "gt", b"c") == "PRUNE"
    # 区间相交必须扫
    assert can_prune(claim, "BYTE_ARRAY", "eq", b"alpha0000") == "SCAN"


def test_truncated_one_sided_only():
    claim = make_claim(10, 20, maxt=True)
    # 下界精确：value < 10 可剪
    assert can_prune(claim, "INT32", "eq", 9) == "PRUNE"
    # 上界截断：value == 20 不可判定
    assert can_prune(claim, "INT32", "eq", 20) == "UNDECIDABLE"
    assert can_prune(claim, "INT32", "eq", 21) == "PRUNE"


def test_missing_minmax_undecidable():
    claim = Claim(has_min_max=False, has_null_count=True, null_count=0,
                  num_values=100)
    assert can_prune(claim, "INT32", "eq", 5) == "UNDECIDABLE"


# ---- NaN / 有符号零的剪枝语义


def test_float_signed_zero_prune():
    claim = make_claim(0.0, 10.0)
    # 真实范围从 +0.0 开始；-0.0 在其之前（total order）-> 可剪
    assert can_prune(claim, "DOUBLE", "eq", -0.0) == "PRUNE"
    assert can_prune(claim, "DOUBLE", "eq", 0.0) == "SCAN"


# ---- 规则 4 端到端：坏统计被禁用后查询结果仍与暴力全扫一致


@pytest.mark.parametrize("fixture_key,column,pred,value", [
    ("wrong", "id", "eq", 500),
    ("wrong", "id", "lt", 5000),
    ("wrong", "code", "gt", 400),
    ("nan_bad", "f", "eq", 1.5),
    ("sort_bad", "score", "eq", 99),
    ("trunc_bad", "word", "eq", "alpha0000"),
])
def test_query_correct_despite_bad_stats(
    fixture_models, fixture_key, column, pred, value
):
    m = fixture_models[fixture_key]
    audit = audit_file(m)
    assert audit.trusted[column] is False
    report = run_query(m, column, pred, value, audit=audit)

    # 暴力全扫（完全独立于统计）作为参考正确答案
    physical = next(c for c in m.schema if c.path == column).physical_type
    expected = []
    for rg in range(len(m.row_groups)):
        for row in read_column_values(m.path, rg, column):
            if row is None:
                continue
            from colstats.ordering import as_bytes, values_equal
            r = as_bytes(row) if physical.startswith("BYTE") else row
            v = as_bytes(value) if physical.startswith("BYTE") else value
            if pred == "eq":
                hit = values_equal(r, v, physical)
            elif pred == "lt":
                hit = r < v
            elif pred == "gt":
                hit = r > v
            else:
                raise AssertionError(pred)
            if hit:
                expected.append(row)
    assert report.result == expected
    # 禁用剪枝意味着所有行组都实际扫描
    assert all(
        g.scanned_rows == m.row_groups[g.row_group].num_rows
        for g in report.groups
    )


def test_good_stats_actually_prunes_groups(fixture_models):
    m = fixture_models["good"]
    audit = audit_file(m)
    report = run_query(m, "id", "eq", 500_000, audit=audit)
    assert all(g.chunk_decision == "PRUNE" for g in report.groups)
    assert report.result == []
    assert sum(g.scanned_rows for g in report.groups) == 0


def test_truncated_fixture_boundary_queries(fixture_models):
    m = fixture_models["trunc"]
    audit = audit_file(m)
    # eq alph 落在截断下界 -> 不可判定 -> 扫描，但无匹配
    r = run_query(m, "word", "eq", "alph", audit=audit)
    assert r.groups[0].chunk_decision == "UNDECIDABLE"
    assert r.result == []
    # 严格外侧剪枝
    r2 = run_query(m, "word", "gt", "c", audit=audit)
    assert r2.groups[0].chunk_decision == "PRUNE"
    assert r2.result == []
