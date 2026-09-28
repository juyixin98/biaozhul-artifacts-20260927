"""审计内核对各文件夹具的具体断言（结论 + 失败类别 + 可定位位置）。

参考真值由 conftest 中的纯 Python 参考实现独立给出，不来自被测内核。
"""
from __future__ import annotations

from colstats.kernel import audit_file
from colstats.parquet_adapter import read_column_values

from conftest import codes_for, find_locations, ref_max, ref_min, ref_null_count


# ---------------------------------------------------------------- 好统计


def test_good_stats_accepted(fixture_models):
    r = audit_file(fixture_models["good"])
    assert r.verdict == "ACCEPTED"
    assert r.trusted == {"id": True, "amount": True}
    assert all(f.severity.value != "ERROR" for f in r.findings)
    assert "GOOD_STATS" in codes_for(r)


def test_good_stats_values_match_independent_truth(fixture_models):
    m = fixture_models["good"]
    for rg in range(2):
        ids = read_column_values(m.path, rg, "id")
        amounts = read_column_values(m.path, rg, "amount")
        chunk_id = m.row_groups[rg].chunks[0]
        chunk_amt = m.row_groups[rg].chunks[1]
        assert chunk_id.claim.min_claim == ref_min(ids, "INT32") == rg * 10000
        assert chunk_id.claim.max_claim == ref_max(ids, "INT32")
        assert chunk_id.claim.null_count == ref_null_count(ids) == 0
        assert chunk_amt.claim.null_count == ref_null_count(amounts) == 100
        assert chunk_amt.claim.min_claim == -6.0
        assert chunk_amt.claim.max_claim == 6.0


def test_good_stats_multiple_pages_and_aggregation(fixture_models):
    m = fixture_models["good"]
    for rg in m.row_groups:
        for chunk in rg.chunks:
            # 强制出了多页
            assert len(chunk.pages) > 1
            # 每页 null_count 求和 == 块 null_count
            page_null_sum = sum(p.claim.null_count for p in chunk.pages)
            assert page_null_sum == chunk.claim.null_count
            # 每页 num_values 求和 == 块 num_values
            assert sum(p.num_values for p in chunk.pages) == chunk.num_values


# ---------------------------------------------------------------- 坏统计


def test_wrong_stats_rejected_and_untrusted(fixture_models):
    r = audit_file(fixture_models["wrong"])
    assert r.verdict == "REJECTED"
    assert r.trusted == {"id": False, "code": False}


def test_wrong_stats_failure_codes_and_locators(fixture_models):
    r = audit_file(fixture_models["wrong"])
    codes = set(codes_for(r, "ERROR"))
    # 具体失败类别都必须出现
    assert {
        "MIN_MISMATCH", "MAX_MISMATCH", "NULL_COUNT_MISMATCH",
        "PAGE_AGGREGATION_MISMATCH", "PAGE_NULL_SUM_MISMATCH",
    } <= codes

    # 可定位：id 第 0 页有 min 错误
    page0_locs = [
        loc for loc in find_locations(r, "MIN_MISMATCH")
        if loc.get("page") == 0 and loc["column"] == "id" and loc["row_group"] == 0
    ]
    assert page0_locs, "必须能定位到 id rg0 page0 的 min 错误"

    # code 块级 null_count 错误定位在 rg1
    null_locs = find_locations(r, "NULL_COUNT_MISMATCH")
    assert {"row_group": 1, "column": "code"} in null_locs

    # code 第 3 页 max 错误定位
    max_page_locs = [
        loc for loc in find_locations(r, "MAX_MISMATCH")
        if loc.get("page") == 3 and loc["column"] == "code"
    ]
    assert max_page_locs


def test_wrong_stats_actual_data_unchanged(fixture_models):
    # 数据本身必须仍然正确（只是统计被改坏）
    m = fixture_models["wrong"]
    ids0 = read_column_values(m.path, 0, "id")
    codes1 = read_column_values(m.path, 1, "code")
    assert ref_min(ids0, "INT32") == 0
    assert ref_max(ids0, "INT32") == 9999
    assert ref_null_count(codes1) == 0
    assert ref_max(codes1, "INT64") == 499


# ---------------------------------------------------------------- 无统计


def test_no_stats_undecidable(fixture_models):
    r = audit_file(fixture_models["nostats"])
    assert r.verdict == "UNDECIDABLE"
    codes = set(codes_for(r, "WARNING"))
    assert {"NULL_COUNT_MISSING", "MIN_MAX_MISSING"} <= codes
    # 无统计不算损坏：列仍标记可审计（但剪枝必须保守）
    assert all(r.trusted.values())


# ---------------------------------------------------------------- 全 NULL


def test_all_null_accepted(fixture_models):
    r = audit_file(fixture_models["allnull"])
    assert r.verdict == "ACCEPTED"
    m = fixture_models["allnull"]
    for rg in range(2):
        note = m.row_groups[rg].chunks[1]
        amount = m.row_groups[rg].chunks[2]
        assert note.claim.null_count == 1500
        assert amount.claim.null_count == 1500
        # 全 NULL 列无 min/max 是正确状态，不应报 MIN_MISMATCH
        assert not note.claim.has_min_max
        assert not amount.claim.has_min_max


def test_all_null_locators_present(fixture_models):
    r = audit_file(fixture_models["allnull"])
    assert not any(f.code == "MIN_MISMATCH" for f in r.findings)
    assert not any(f.code == "NULL_COUNT_MISMATCH" for f in r.findings)


# ---------------------------------------------------------------- 混合 NaN


def test_mixed_nan_good_accepted(fixture_models):
    m = fixture_models["nan"]
    r = audit_file(m)
    assert r.verdict == "ACCEPTED"
    vals = read_column_values(m.path, 0, "f")
    # 独立参考真值（每个 6000 行行组：600 个 NULL）
    assert ref_min(vals, "DOUBLE") == float("-inf")
    assert ref_max(vals, "DOUBLE") == float("inf")
    assert ref_null_count(vals) == 600
    chunk = m.row_groups[0].chunks[0]
    assert chunk.claim.min_claim == float("-inf")
    assert chunk.claim.max_claim == float("inf")
    assert chunk.claim.null_count == 600


def test_mixed_nan_bad_rejected(fixture_models):
    r = audit_file(fixture_models["nan_bad"])
    assert r.verdict == "REJECTED"
    assert r.trusted == {"f": False}
    codes = set(codes_for(r, "ERROR"))
    assert "MAX_MISMATCH" in codes
    # 坏 max 被声明为 NaN，而真值 max 是 +Inf
    locs = find_locations(r, "MAX_MISMATCH")
    assert any(loc["column"] == "f" for loc in locs)


def test_mixed_nan_truth_contains_signed_zero_and_nan(fixture_models):
    from colstats.kernel import compute_truth

    m = fixture_models["nan"]
    vals = read_column_values(m.path, 0, "f")
    truth = compute_truth(vals, "DOUBLE")
    assert truth.contains_nan is True
    assert truth.contains_negative_zero is True


# ---------------------------------------------------------------- 截断字符串


def test_truncated_good_accepted(fixture_models):
    r = audit_file(fixture_models["trunc"])
    assert r.verdict == "ACCEPTED"
    assert r.truncated_columns == ["word"]


def test_truncated_bad_rejected_with_direction_code(fixture_models):
    r = audit_file(fixture_models["trunc_bad"])
    assert r.verdict == "REJECTED"
    assert "TRUNCATED_BOUND_OUTSIDE" in set(codes_for(r, "ERROR"))
    locs = find_locations(r, "TRUNCATED_BOUND_OUTSIDE")
    assert any(loc.get("bound") == "max" for loc in locs)


# ---------------------------------------------------------------- 排序声明


def test_sorting_wrong_rejected(fixture_models):
    r = audit_file(fixture_models["sort_bad"])
    assert r.verdict == "REJECTED"
    assert r.trusted["score"] is False
    locs = find_locations(r, "SORTING_DECLARATION_VIOLATED")
    assert locs and locs[0]["column"] == "score"


def test_sorting_good_column_still_trusted(fixture_models):
    # sorting_wrong 里 id 列与统计没问题，只有 score 被标记不可信
    r = audit_file(fixture_models["sort_bad"])
    assert r.trusted["id"] is True
    assert r.trusted["score"] is False
