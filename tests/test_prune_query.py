"""剪枝与查询正确性测试: 禁用坏统计后结果必须与独立 oracle 全扫一致。"""
import math

import pytest

from colaudit import adapter as ad
from colaudit.audit import OK, audit_dataset
from colaudit.catalog import Catalog
from colaudit.prune import Predicate, decide_page
from colaudit.query import execute, full_scan_counts
from fixtures import oracle_stats
from oracle import load_ground_truth, oracle_predicate, pages


def _verdicts_by_page(report):
    out = {}
    for v in report["verdicts"]:
        if v["scope"] != "page":
            continue
        out.setdefault((v["file"], v["row_group"], v["page"]), {})[
            v["column_name"]
        ] = v
    return out


def _run(ds, col, op, value=None, request_id="t"):
    report = audit_dataset(ds, request_id=request_id, mask_sensitive=False)
    pred = Predicate(column=col, op=op, value=value)
    result = execute(
        ds,
        pred,
        _verdicts_by_page(report),
        request_id=request_id,
        redact=False,
    )
    return report, pred, result


@pytest.mark.parametrize("op,value", [
    ("gt", 5.0), ("ge", 6.0), ("lt", 2.0), ("le", 1.0),
    ("eq", 3.0), ("ne", 3.0), ("is_null", None), ("not_null", None),
])
def test_well_formed_query_matches_oracle(fixture_root, load, op, value):
    ds = load(fixture_root, "well_formed")
    truth = load_ground_truth(fixture_root / "well_formed")
    expected = oracle_predicate(truth, "score", op, value)
    _, _, result = _run(ds, "score", op, value)
    assert result.matched_rows == len(expected), (op, value)
    assert [r["id"] for r in result.rows] == [r["id"] for r in expected]


@pytest.mark.parametrize("op,value", [
    ("gt", 0.0), ("eq", 1.0), ("lt", 5.0), ("is_null", None),
])
def test_bad_statistics_disabled_and_still_correct(
    fixture_root, load, op, value
):
    """核心验收: 坏统计页不剪枝 (全扫), 命中集合仍与 oracle 一致。"""
    ds = load(fixture_root, "bad_statistics")
    truth = load_ground_truth(fixture_root / "bad_statistics")
    expected = oracle_predicate(truth, "score", op, value)
    report, _, result = _run(ds, "score", op, value)

    assert result.matched_rows == len(expected)
    assert [r["id"] for r in result.rows] == [r["id"] for r in expected]

    # 坏页 (rg0/page0 minmax, rg0/page1 null_count) 必须出现在
    # "因不信任而扫描" 的轨迹里, 不能被跳过
    trace = {
        (t["file"], t["row_group"], t["page"]): t
        for t in result.page_trace
    }
    for bad_page in (("data.parquet", 0, 0), ("data.parquet", 0, 1)):
        assert trace[bad_page]["action"] == "scanned_untrusted"
        assert "禁用剪枝" in trace[bad_page]["reason"]

    # id 严格升序: 对 id 的大阈值 gt 查询, 受信页必须真正发生剪枝
    _, _, id_result = _run(ds, "id", "gt", 110)
    assert id_result.matched_rows == len(
        oracle_predicate(truth, "id", "gt", 110)
    )
    assert any(t["action"] == "skipped_pruned"
               for t in id_result.page_trace)


def test_no_statistics_scans_every_page(fixture_root, load):
    ds = load(fixture_root, "no_statistics")
    truth = load_ground_truth(fixture_root / "no_statistics")
    expected = oracle_predicate(truth, "score", "gt", 100.0)
    assert expected == []  # 任何行都不满足
    _, _, result = _run(ds, "score", "gt", 100.0)
    assert result.matched_rows == 0
    # 没有受信统计 -> 即使谓词明显不命中也不允许跳过任何页
    assert result.pages_skipped == 0
    assert result.pages_scanned == result.pages_total == 6


def test_all_null_is_null_pruning(fixture_root, load):
    ds = load(fixture_root, "all_null")
    truth = load_ground_truth(fixture_root / "all_null")
    # score is_null -> 全部命中
    _, _, result = _run(ds, "score", "is_null")
    assert result.matched_rows == len(truth) == 120
    # score gt 1: 全 NULL 页可安全跳过 (比较谓词不命中 NULL)
    _, _, result2 = _run(ds, "score", "gt", 1.0)
    assert result2.matched_rows == 0
    assert result2.pages_skipped == 6


def test_mixed_nan_predicate_semantics(fixture_root, load):
    ds = load(fixture_root, "mixed_nan")
    truth = load_ground_truth(fixture_root / "mixed_nan")
    # NaN 不等于任何值: ne 0.0 应包含 NaN? 不 — SQL 三值逻辑 NaN 比较 UNKNOWN
    # 本系统约定 NaN 对六谓词不命中; 用 oracle 同样语义核对
    for op, value in [("ge", 2.0), ("eq", 0.0), ("ne", 0.0),
                      ("is_null", None), ("not_null", None)]:
        expected = oracle_predicate(truth, "score", op, value)
        _, _, result = _run(ds, "score", op, value)
        ids = [r["id"] for r in result.rows]
        assert ids == [r["id"] for r in expected], (op, value)
        assert result.matched_rows == len(expected)

    # 含 NaN 的页在 ge/gt 类谓词下仍可按区间剪枝, NaN 行不被误带入
    _, _, result = _run(ds, "score", "gt", 3.0)
    expected = oracle_predicate(truth, "score", "gt", 3.0)
    assert [r["id"] for r in result.rows] == [r["id"] for r in expected]


def test_truncated_min_does_not_block_lower_bound_prune(fixture_root, load):
    """合法截断: name lt 一个小值时, 截断 min 仍是可信下界, 可剪枝。"""
    ds = load(fixture_root, "truncated_string")
    report = audit_dataset(ds, mask_sensitive=False)
    claimed = ds.claimed_page_stats("data.parquet", 0, 0, "name")
    assert claimed.min_truncated is True
    # lt 谓词: 若 target <= 截断下界, 页可跳过
    decision = decide_page(
        Predicate("name", "lt", "aaaa"), claimed,
        file="data.parquet", row_group=0, page=0,
    )
    assert decision.skipped is True


def test_truncated_max_forces_scan_for_upper_bound(fixture_root, load):
    """max 截断时 gt/ge 必须保守扫描 (验收规则 2)。"""
    from colaudit.stats import ColumnStats

    st = ColumnStats.from_json(
        oracle_stats(
            load_ground_truth(fixture_root / "truncated_string")[20:40],
            "name",
        )
    )
    st.max_truncated = True
    d1 = decide_page(Predicate("name", "gt", "zzz"), st,
                     file="f", row_group=0, page=2)
    assert d1.skipped is False
    assert "截断" in d1.reason
    d2 = decide_page(Predicate("name", "ge", "zzz"), st,
                     file="f", row_group=0, page=2)
    assert d2.skipped is False


def test_invalid_truncated_stats_never_used(fixture_root, load):
    """非法截断夹具: 页不受信 -> 查询全扫且结果仍与 oracle 一致。"""
    ds = load(fixture_root, "truncated_invalid")
    truth = load_ground_truth(fixture_root / "truncated_invalid")
    _, _, result = _run(ds, "name", "lt", "aaaa")
    expected = oracle_predicate(truth, "name", "lt", "aaaa")
    assert [r["id"] for r in result.rows] == [r["id"] for r in expected]
    # rg0/page0 不受信必须扫描
    trace = {
        (t["file"], t["row_group"], t["page"]): t
        for t in result.page_trace
    }
    assert trace[("data.parquet", 0, 0)]["action"] == "scanned_untrusted"


def test_baseline_full_scan_agreement(fixture_root, load):
    ds = load(fixture_root, "mixed_nan")
    total, matched = full_scan_counts(ds, Predicate("score", "ge", 2.0))
    truth = load_ground_truth(fixture_root / "mixed_nan")
    assert total == len(truth)
    assert matched == len(oracle_predicate(truth, "score", "ge", 2.0))
