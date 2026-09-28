"""内核保守裁剪单元测试：直接构造 TableMetadata，精确断言裁剪原因码与层级。"""
from __future__ import annotations

import pytest

from pruning import transforms as T
from pruning.kernel import PruningKernel, candidate_month_buckets
from pruning.model import (Certainty, ColumnStats, Decision, FileEntry, Layer,
                           PartitionEntry, Predicate, PruneReason, TableMetadata)


def mkfile(fid, month, lo, hi, nulls=0, rows=3, col="amount", ctype="int64",
           present=True, truncated=False):
    stats = {col: ColumnStats(column=col, type=ctype, min_value=lo, max_value=hi,
                              null_count=nulls, row_count=rows, present=present,
                              truncated=truncated)}
    return FileEntry(fid, f"/t/{month}/{fid}", month, rows, stats)


def mkmeta(files_by_month, extra_cols=("amount",)):
    parts = [PartitionEntry(column="ts", value=m, files=fs)
             for m, fs in files_by_month.items()]
    cols = {"ts": "int64"}
    for c in extra_cols:
        cols[c] = "int64"
    return TableMetadata(
        table="t", partition_column="ts", transform=T.transform_identity(),
        partitions=parts, columns=cols)


KERNEL = PruningKernel(T.DATE_TRANSFORM_VERSION)


def find(plan, fid, layer=None):
    for d in plan.decisions:
        if d.target_id == fid and (layer is None or d.layer is layer):
            return d
    raise AssertionError(f"no decision for {fid} layer={layer}")


# ---- 候选桶反推：桶值不是原值 ----

def test_candidate_buckets_date_range_spans_boundary_months():
    p = Predicate.range_("ts", "2024-02-10", "2024-02-20", True, True)
    buckets = candidate_month_buckets(p)
    # 关键：绝不能把 "2024-02" 当数字与边界比较；候选是反推出的桶
    assert buckets == [(2024, 2)]


def test_candidate_buckets_negative_month():
    p = Predicate.range_("ts", "1969-12-01", "1969-12-31", True, True)
    assert candidate_month_buckets(p) == [(1969, 12)]


def test_open_upper_at_month_start_excludes_that_month():
    # [2024-02-01, 2024-03-01) ：3 月桶可安全排除
    p = Predicate.range_("ts", "2024-02-01", "2024-03-01", True, False)
    assert candidate_month_buckets(p) == [(2024, 2)]


# ---- 分区层 ----

def test_partition_prunes_whole_months_outside_range():
    meta = mkmeta({
        "2024-01": [mkfile("f1", "2024-01", 1, 2)],
        "2024-02": [mkfile("f2", "2024-02", 3, 4)],
        "2024-03": [mkfile("f3", "2024-03", 5, 6)],
    })
    plan = KERNEL.plan(meta, [Predicate.range_("ts", "2024-02-01", "2024-02-28", True, True)], "r1")
    d1 = find(plan, "ts=2024-01", Layer.PARTITION)
    d3 = find(plan, "ts=2024-03", Layer.PARTITION)
    assert d1.certainty is Certainty.PRUNED
    assert d1.reason is PruneReason.PARTITION_OUTSIDE_RANGE
    assert d3.certainty is Certainty.PRUNED
    assert plan.selected_files == ["f2"]
    assert plan.totals["files_pruned_by_partition"] == 2


def test_is_null_not_pruned_at_partition_layer():
    meta = mkmeta({"2024-02": [mkfile("f1", "2024-02", 3, 4, nulls=1)]})
    plan = KERNEL.plan(meta, [Predicate.is_null("ts")], "r2")
    # 分区层保留
    pd = find(plan, "ts=2024-02", Layer.PARTITION)
    assert pd.certainty is Certainty.KEPT
    # 该文件含 null -> stats 层也保留
    assert plan.selected_files == ["f1"]


def test_is_null_prunes_files_with_zero_nulls_at_stats_layer():
    f_yes = mkfile("fy", "2024-02", 3, 4, nulls=1, col="ts")
    f_no = mkfile("fn", "2024-02", 3, 4, nulls=0, col="ts")
    meta = mkmeta({"2024-02": [f_yes, f_no]})
    plan = KERNEL.plan(meta, [Predicate.is_null("ts")], "r3")
    d = find(plan, "fn", Layer.FILE_STATS)
    assert d.certainty is Certainty.PRUNED
    assert d.reason is PruneReason.STATS_NO_NULL_VS_IS_NULL
    assert plan.selected_files == ["fy"]


def test_not_null_prunes_all_null_files():
    f_all = mkfile("fa", "2024-02", None, None, nulls=3, rows=3, col="ts")
    f_mix = mkfile("fm", "2024-02", 3, 4, nulls=1, col="ts")
    meta = mkmeta({"2024-02": [f_all, f_mix]})
    plan = KERNEL.plan(meta, [Predicate.not_null("ts")], "r4")
    d = find(plan, "fa", Layer.FILE_STATS)
    assert d.certainty is Certainty.PRUNED
    assert d.reason is PruneReason.STATS_ALL_NULL_VS_NOT_NULL
    assert plan.selected_files == ["fm"]


# ---- 文件 stats 层 ----

def test_stats_below_lower_and_above_upper():
    f_lo = mkfile("lo", "2024-02", 0, 9)
    f_hi = mkfile("hi", "2024-02", 100, 200)
    f_in = mkfile("in", "2024-02", 40, 60)
    meta = mkmeta({"2024-02": [f_lo, f_hi, f_in]})
    plan = KERNEL.plan(meta, [Predicate.range_("amount", 20, 80, True, True)], "r5")
    assert find(plan, "lo", Layer.FILE_STATS).reason is PruneReason.STATS_BELOW_LOWER
    assert find(plan, "hi", Layer.FILE_STATS).reason is PruneReason.STATS_ABOVE_UPPER
    assert plan.selected_files == ["in"]


def test_eq_numeric_point_prunes_disjoint_files():
    f = mkfile("f", "2024-02", 100, 200)
    meta = mkmeta({"2024-02": [f]})
    plan = KERNEL.plan(meta, [Predicate.eq("amount", 300)], "r6")
    assert find(plan, "f", Layer.FILE_STATS).reason is PruneReason.STATS_EQ_NO_OVERLAP
    assert plan.selected_files == []


def test_in_prunes_span_disjoint():
    f = mkfile("f", "2024-02", 0, 9)
    meta = mkmeta({"2024-02": [f]})
    plan = KERNEL.plan(meta, [Predicate.in_("amount", [100, 200])], "r7")
    assert find(plan, "f", Layer.FILE_STATS).reason is PruneReason.STATS_IN_NO_OVERLAP


# ---- 保守保留：缺失 / 截断 / null_count 未知 ----

def test_missing_stats_keeps_file():
    f = mkfile("f", "2024-02", 0, 9, present=False)
    meta = mkmeta({"2024-02": [f]})
    plan = KERNEL.plan(meta, [Predicate.range_("amount", 100, 200, True, True)], "r8")
    d = find(plan, "f", Layer.FILE_STATS)
    assert d.certainty is Certainty.KEPT
    assert d.reason is PruneReason.KEPT_STATS_MISSING
    assert plan.selected_files == ["f"]


def test_truncated_stats_keeps_file():
    f = mkfile("f", "2024-02", "a", "zzzz", col="region", ctype="string", truncated=True)
    meta = mkmeta({"2024-02": [f]}, extra_cols=("amount",))
    meta.columns["region"] = "string"
    plan = KERNEL.plan(meta, [Predicate.eq("region", "aaaa-unknown-tail")], "r9")
    d = find(plan, "f", Layer.FILE_STATS)
    assert d.certainty is Certainty.KEPT
    assert d.reason is PruneReason.KEPT_STATS_TRUNCATED


def test_unknown_null_count_keeps_for_null_predicate():
    col = ColumnStats("ts", "int64", 1, 9, null_count=None, row_count=3)
    f = FileEntry("f", "/p/f", "2024-02", 3, {"ts": col})
    meta = mkmeta({"2024-02": [f]})
    plan = KERNEL.plan(meta, [Predicate.is_null("ts")], "r10")
    d = find(plan, "f", Layer.FILE_STATS)
    assert d.reason is PruneReason.KEPT_NULL_COUNT_UNKNOWN
    assert plan.selected_files == ["f"]


def test_missing_column_stats_keeps_file():
    f = FileEntry("f", "/p/f", "2024-02", 3, {})
    meta = mkmeta({"2024-02": [f]})
    plan = KERNEL.plan(meta, [Predicate.range_("other", 0, 1, True, True)], "r11")
    d = find(plan, "f", Layer.FILE_STATS)
    assert d.certainty is Certainty.KEPT
    assert d.reason is PruneReason.KEPT_STATS_MISSING


def test_and_predicates_must_all_allow_file():
    # ts 在范围内但 amount 超界 -> 裁剪
    f = mkfile("f", "2024-02", 3, 4)
    f.stats["amount"] = ColumnStats("amount", "int64", 1000, 2000, 0, 3)
    meta = mkmeta({"2024-02": [f]})
    meta.columns["amount"] = "int64"
    plan = KERNEL.plan(meta, [Predicate.range_("ts", 0, 100, True, True),
                              Predicate.range_("amount", 0, 10, True, True)], "r12")
    assert plan.selected_files == []
