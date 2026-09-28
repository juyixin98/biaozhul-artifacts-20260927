"""审计内核测试: 五类夹具的裁决、失败类别、聚合关系与诊断要求。"""
import pytest

from colaudit import adapter as ad
from colaudit.audit import (
    AGGREGATION_MISMATCH,
    EMBEDDED_INCONCLUSIVE,
    MINMAX_MISMATCH,
    NULL_COUNT_MISMATCH,
    OK,
    SORTED_MISMATCH,
    STATS_MISSING,
    TRUNCATION_INVALID,
    audit_dataset,
)


def _find(verdicts, *, file="data.parquet", rg=None, scope=None,
          page=None, column=None):
    out = []
    for v in verdicts:
        if v["file"] != file:
            continue
        if rg is not None and v["row_group"] != rg:
            continue
        if scope is not None and v["scope"] != scope:
            continue
        if page is not None and v.get("page") != page:
            continue
        if column is not None and v["column_name"] != column:
            continue
        out.append(v)
    return out


def test_well_formed_all_trusted(fixture_root, load):
    ds = load(fixture_root, "well_formed")
    report = audit_dataset(ds, mask_sensitive=False)
    bad = [v for v in report["verdicts"] if v["verdict"] != OK]
    assert bad == [], [
        (v["file"], v["row_group"], v["scope"], v.get("page"),
         v["column_name"], v["verdict"]) for v in bad
    ]
    assert report["summary"]["all_trusted"] is True
    # 每列 x (6 页 + 2 行组) = 32 条裁决
    assert len(report["verdicts"]) == 4 * 8
    # 行组接受事件必须说明理由并携带状态
    accepts = [
        e for e in report["diagnostics"]
        if e["scope"] == "row_group" and e["decision"] == "accept"
    ]
    assert len(accepts) == 8
    assert all(e["state"] for e in accepts)


def test_bad_statistics_pinpoint_locations(fixture_root, load):
    ds = load(fixture_root, "bad_statistics")
    report = audit_dataset(ds, mask_sensitive=False)
    v = report["verdicts"]

    # (rg0, page0, score) min 被污染
    p0 = _find(v, rg=0, scope="page", page=0, column="score")[0]
    assert p0["verdict"] == MINMAX_MISMATCH
    assert p0["trusted"] is False
    assert any("min" in d for d in p0["detail"]["diffs"])

    # 行组 0 score: min 同样错误
    rg0 = _find(v, rg=0, scope="row_group", column="score")[0]
    assert rg0["verdict"] == MINMAX_MISMATCH

    # (rg0, page1, score) null_count 虚增
    p1 = _find(v, rg=0, scope="page", page=1, column="score")[0]
    assert p1["verdict"] == NULL_COUNT_MISMATCH

    # 行组 0 score 的页聚合 != 行组声明 -> 聚合不成立
    # (行组本身 min 错误已先被判 MINMAX_MISMATCH; 这里单独验证页 1 定位)
    loc = [
        e for e in report["diagnostics"]
        if e["file"] == "data.parquet" and e["row_group"] == 0
        and e["scope"] == "page" and e["page"] == 1
        and e["column_name"] == "score"
    ]
    assert len(loc) == 1 and loc[0]["decision"] == "reject"
    assert "null_count" in str(loc[0]["state"]["detail"])

    # 行组 1 id sorted 错误
    rg1 = _find(v, rg=1, scope="row_group", column="id")[0]
    assert rg1["verdict"] == SORTED_MISMATCH

    # 其余页必须保持受信 (坏统计不牵连)
    score_pages_ok = [
        x for x in _find(v, scope="page", column="score")
        if (x["row_group"], x["page"]) not in {(0, 0), (0, 1)}
    ]
    assert all(x["verdict"] == OK for x in score_pages_ok)

    summary = report["summary"]
    assert summary["trusted"] < summary["total_columns_scopes"]
    assert summary["by_verdict"][MINMAX_MISMATCH] >= 2
    assert summary["by_verdict"][SORTED_MISMATCH] >= 1


def test_no_statistics_all_inconclusive_never_trusted(fixture_root, load):
    ds = load(fixture_root, "no_statistics")
    assert not (fixture_root / "no_statistics" / "claims.json").exists()
    report = audit_dataset(ds)
    assert all(v["trusted"] is False for v in report["verdicts"])
    pages = _find(report["verdicts"], scope="page")
    assert all(v["verdict"] == STATS_MISSING for v in pages)
    rgs = _find(report["verdicts"], scope="row_group")
    assert all(v["verdict"] == STATS_MISSING for v in rgs)
    events = report["diagnostics"]
    assert all(e["decision"] == "unknown" for e in events)
    assert all("拒绝剪枝" in e["message"] for e in events)


def test_all_null_dataset(fixture_root, load):
    ds = load(fixture_root, "all_null")
    report = audit_dataset(ds, mask_sensitive=False)
    # score/name/active 全 NULL, 统计本身正确 -> 受信
    for col in ("score", "name", "active"):
        for v in _find(report["verdicts"], column=col):
            assert v["verdict"] == OK, (col, v)
    p = _find(report["verdicts"], scope="page", column="score")[0]
    assert p["detail"]  # 有判定依据


def test_mixed_nan_and_signed_zero(fixture_root, load):
    ds = load(fixture_root, "mixed_nan")
    report = audit_dataset(ds, mask_sensitive=False)
    bad = [v for v in report["verdicts"] if v["verdict"] != OK]
    assert bad == [], [(v["column_name"], v["verdict"]) for v in bad]
    # 找到一个含 NaN 的页诊断, 状态必须包含 nan_count
    score_events = [
        e for e in report["diagnostics"]
        if e["column_name"] == "score" and e["scope"] == "page"
    ]
    nan_events = [
        e for e in score_events
        if e["state"]["actual"].get("nan_count", 0) > 0
    ]
    assert nan_events, "至少一个页应含 NaN"


def test_truncation_legitimate_vs_invalid(fixture_root, load):
    good = audit_dataset(load(fixture_root, "truncated_string"),
                         mask_sensitive=False)
    bad = audit_dataset(load(fixture_root, "truncated_invalid"),
                        mask_sensitive=False)
    # 合法截断: 页 0 name 受信, 状态保留截断标志
    gp = _find(good["verdicts"], rg=0, scope="page", page=0,
               column="name")[0]
    assert gp["verdict"] == OK
    assert good["run_id"]  # 有运行标识

    bp = _find(bad["verdicts"], rg=0, scope="page", page=0,
               column="name")[0]
    assert bp["verdict"] == TRUNCATION_INVALID
    assert bp["trusted"] is False
    rg = _find(bad["verdicts"], rg=0, scope="row_group", column="name")[0]
    assert rg["verdict"] in {TRUNCATION_INVALID, AGGREGATION_MISMATCH}


def test_diagnostics_carry_request_id_and_locator(fixture_root, load):
    ds = load(fixture_root, "bad_statistics")
    report = audit_dataset(ds, request_id="req-xyz", mask_sensitive=False)
    assert report["request_id"] == "req-xyz"
    assert all(e["request_id"] == "req-xyz"
               for e in report["diagnostics"])
    rejects = [e for e in report["diagnostics"]
               if e["decision"] == "reject"]
    assert rejects
    for e in rejects:
        assert e["file"] and e["column_name"]
        assert e["row_group"] is not None
        assert e["code"] and e["message"]
