"""端到端测试：真实 PyArrow Parquet 夹具 + 独立全扫描 oracle。

断言具体结果（选哪些文件、匹配行数、分层裁剪量、失败类别），
参考真值由 FullScanValidator 用 PyArrow Compute 逐行独立计算，
不经被测内核生成。
"""
from __future__ import annotations

import os
import pytest

from pruning.config import Config
from pruning.schemas import RegisterIn, ValidateIn
from pruning.service import PruningService
from pruning.validation import FailureCategory
from tools import make_fixtures


@pytest.fixture()
def svc(tmp_path):
    root = str(tmp_path / "data")
    make_fixtures.build(root)
    cfg = Config(data_root=root, db_path=str(tmp_path / "cat.sqlite"))
    service = PruningService(cfg)
    service.register(RegisterIn(
        table="events", truncated_string_columns=["region"], truncate_prefix_len=4))
    return service


def _val(svc, preds, rid):
    return svc.validate(ValidateIn(table="events", request_id=rid, predicates=preds))


def _names(ids):
    return sorted(i.split("/")[-1] for i in ids)


def test_register_counts(svc):
    md = svc.catalog.load_table("events")
    assert len(md.partitions) == 4
    assert sum(len(p.files) for p in md.partitions) == 6
    # 每文件 3 列统计
    for _, f in md.iter_files():
        assert set(f.stats) == {"event_ts", "amount", "region"}


def test_month_partition_date_range_zero_miss(svc):
    r = _val(svc, [{"column": "event_ts", "kind": "range",
                    "lower": "2024-02-01", "upper": "2024-02-29",
                    "upper_inclusive": True}], "e2e-feb")
    assert r["status"] == "pass"
    assert r["zero_missed_matches"] is True
    assert r["rows_matched_full_scan"] == 5
    assert _names(r["kernel_selected_files"]) == [
        "part-2024-02-a.parquet", "part-2024-02-b.parquet"]
    assert r["layer_pruning"]["files_pruned_by_partition"] == 4
    assert r["layer_pruning"]["files_pruned_by_stats"] == 0
    hard = [f for f in r["failures"] if f["category"] != FailureCategory.SELECTED_BUT_EMPTY_MATCH]
    assert hard == []


def test_negative_timestamp_partition(svc):
    r = _val(svc, [{"column": "event_ts", "kind": "range",
                    "lower": "1969-12-01", "upper": "1969-12-31",
                    "upper_inclusive": True}], "e2e-neg")
    assert r["status"] == "pass"
    assert r["zero_missed_matches"] is True
    assert _names(r["truly_matching_files"]) == ["part-1969-12-a.parquet"]
    assert _names(r["kernel_selected_files"]) == ["part-1969-12-a.parquet"]
    assert r["layer_pruning"]["files_pruned_by_partition"] == 5
    assert r["rows_matched_full_scan"] == 2  # NULL 不算匹配


def test_null_predicate_uses_stats_layer(svc):
    r = _val(svc, [{"column": "event_ts", "kind": "is_null"}], "e2e-isnull")
    assert r["status"] == "pass"
    # 三个文件各含一个 NULL 时间戳
    assert r["rows_matched_full_scan"] == 3
    assert _names(r["truly_matching_files"]) == [
        "part-1969-12-a.parquet", "part-2024-01-late.parquet", "part-2024-02-b.parquet"]
    assert _names(r["kernel_selected_files"]) == _names(r["truly_matching_files"])
    # NULL 不在分区层裁
    assert r["layer_pruning"]["files_pruned_by_partition"] == 0
    assert r["layer_pruning"]["files_pruned_by_stats"] == 3


def test_truncated_string_stats_force_keep(svc):
    # region 统计被截断到前 4 字符；长字符串真实极值未知
    r = _val(svc, [{"column": "region", "kind": "eq",
                    "value": "longregion-ap-east-9999"}], "e2e-trunc")
    assert r["status"] == "pass"
    assert r["zero_missed_matches"] is True
    # 真正匹配的是 02-b，且必须被保留
    assert "part-2024-02-b.parquet" in _names(r["kernel_selected_files"])
    # 由于截断，内核一个文件都不能裁
    assert r["layer_pruning"]["files_pruned_by_stats"] == 0
    assert r["layer_pruning"]["files_pruned_by_partition"] == 0


def test_non_truncated_string_eq_prunes(svc, tmp_path):
    # 用一份"不截断统计"的注册，验证字符串能在 stats 层精确裁剪
    from pruning.adapter import ReadOptions, discover_table
    md = discover_table(svc.config.data_root, "events", "event_ts", ReadOptions())
    svc.catalog.register_table(md, svc.config.data_root)
    r = _val(svc, [{"column": "region", "kind": "eq", "value": "eu-west"}], "e2e-str-eq")
    assert r["status"] == "pass"
    names = _names(r["kernel_selected_files"])
    # 真实匹配的三个文件必须全部保留（零漏行）
    for must in ["part-1969-12-a.parquet", "part-2024-01-late.parquet",
                 "part-2024-02-b.parquet"]:
        assert must in names
    # early（区间全为 us-*，min=us-east > eu-west）与 03（min=us-east）可精确裁掉
    assert "part-2024-01-early.parquet" not in names
    assert "part-2024-03-a.parquet" not in names
    assert r["layer_pruning"]["files_pruned_by_stats"] == 2
    # 02-a 的 [ap-south,us-east] 极值区间覆盖 eu-west，按 min/max 无法排除，
    # 属于合法保守冗余，必须被标为 selected_no_match 而非漏行
    redundant = {f["file_id"].split("/")[-1] for f in r["failures"]
                 if f["category"] == FailureCategory.SELECTED_BUT_EMPTY_MATCH}
    assert "part-2024-02-a.parquet" in redundant


def test_and_of_partition_and_stats(svc):
    r = _val(svc, [
        {"column": "event_ts", "kind": "range", "lower": "2024-02-01",
         "upper": "2024-02-29", "upper_inclusive": True},
        {"column": "amount", "kind": "range", "lower": 400},
    ], "e2e-and")
    assert r["status"] == "pass"
    assert _names(r["kernel_selected_files"]) == ["part-2024-02-b.parquet"]
    assert r["layer_pruning"]["files_pruned_by_partition"] == 4
    assert r["layer_pruning"]["files_pruned_by_stats"] == 1


def test_numeric_range_stats_layer(svc):
    r = _val(svc, [{"column": "amount", "kind": "range", "lower": 450}], "e2e-amt")
    assert r["status"] == "pass"
    assert _names(r["kernel_selected_files"]) == ["part-2024-03-a.parquet"]
    assert r["layer_pruning"]["files_pruned_by_stats"] == 5


def test_open_boundary_at_month_start(svc):
    # [2024-02-01 00:00 UTC, 2024-03-01 00:00 UTC) 数值开界：3 月整桶排除
    import pruning.transforms as T
    lo = T.date_range_epoch_bounds("2024-02-01", "2024-02-01")[0]
    hi = T.date_range_epoch_bounds("2024-03-01", "2024-03-01")[0]
    r = _val(svc, [{"column": "event_ts", "kind": "range", "lower": lo,
                    "upper": hi, "lower_inclusive": True,
                    "upper_inclusive": False}], "e2e-open")
    assert r["status"] == "pass"
    assert r["zero_missed_matches"] is True
    assert not any("2024-03" in f for f in r["kernel_selected_files"])


def test_unknown_column_reported_as_failure(svc):
    r = _val(svc, [{"column": "nope", "kind": "is_null"}], "e2e-unk")
    cats = [f["category"] for f in r["failures"]]
    assert FailureCategory.UNKNOWN_COLUMN in cats
    assert r["status"] == "fail"


def test_audit_persisted_with_reasons(svc):
    _val(svc, [{"column": "event_ts", "kind": "range",
                "lower": "2024-02-01", "upper": "2024-02-29",
                "upper_inclusive": True}], "e2e-audit")
    audit = svc.catalog.get_audit("e2e-audit")
    assert audit is not None
    reasons = {d["reason"] for d in audit["decisions"]}
    assert "partition_outside_range" in reasons
    # 每条决策都带证据与层级
    for d in audit["decisions"]:
        assert d["layer"] in {"partition", "file_stats"}
        assert d["detail"]
