"""Kernel tests against synthetic, hand-built statistics.

Stats are constructed directly (not produced by the pruner), so expected
verdicts are computed by the test author, not by the system under test. Each
test asserts the exact verdict AND the reason code category.
"""

from __future__ import annotations

import datetime as dt

import pytest

from prune import values as V
from prune.kernel import (Column, ColumnStat, Decision, FileStat, PartitionInfo,
                          Reason, TableContext, Verdict, plan_prune,
                          _eval_leaf_on_stats)
from prune.models import Leaf, Node, Op, parse_predicate
from prune.transforms import MonthTransform, TRANSFORM_SPEC_VERSION, tzdb_version

UTC = dt.timezone.utc
TS = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
T = Column("ts", V.DATETIME)
N = Column("name", V.STR)
A = Column("amount", V.INT)


def fs_stat(minv=None, maxv=None, nulls=0, mint=False, maxt=False,
            present=True):
    return ColumnStat(minimum=minv, maximum=maxv, null_count=nulls,
                      min_truncated=mint, max_truncated=maxt, present=present)


def eval_leaf(op, col, cs, value=None, negated=False, num_rows=10):
    leaf = Leaf(op=op, column=col.name, value=value, negated=negated)
    return _eval_leaf_on_stats(leaf, cs, col, num_rows)


# --------------------------------------------------------------------------- #
# File-stat level
# --------------------------------------------------------------------------- #

def test_ge_prunes_when_sound_max_below_literal():
    # literal 2024-03-05 00:00 +08 == 2024-03-04 16:00 UTC; stored max is below
    cs = fs_stat(TS("2024-03-01T00:00:00Z"), TS("2024-03-04T15:59:59Z"))
    d = eval_leaf(Op.GE, T, cs, "2024-03-05T00:00:00+08:00")
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_ABOVE_MAX.value


def test_gt_max_equals_literal_is_unknown_boundary():
    # max == literal for GT: a greater value cannot be proven, but absence
    # cannot be proven from a single bound either => conservative UNKNOWN.
    cs = fs_stat(TS("2024-03-01T00:00:00Z"), TS("2024-03-05T00:00:00Z"))
    d = eval_leaf(Op.GT, T, cs, "2024-03-05T00:00:00Z")
    assert d.verdict is Verdict.UNKNOWN
    assert d.reasons[0].code == Reason.FILE_BOUNDARY_INCONCLUSIVE.value


def test_ge_max_equals_literal_is_kept():
    cs = fs_stat(TS("2024-03-01T00:00:00Z"), TS("2024-03-05T00:00:00Z"))
    d = eval_leaf(Op.GE, T, cs, "2024-03-05T00:00:00Z")
    assert d.verdict is Verdict.KEPT


def test_le_prunes_when_sound_min_above_literal():
    cs = fs_stat(TS("2024-03-10T00:00:00Z"), TS("2024-03-11T00:00:00Z"))
    d = eval_leaf(Op.LT, T, cs, "2024-03-06T00:00:00+08:00")
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_BELOW_MIN


def test_negative_timestamp_ranges_prune_correctly():
    cs = fs_stat(TS("1969-12-01T00:00:00Z"), TS("1969-12-10T00:00:00Z"))
    d = eval_leaf(Op.GE, T, cs, "1970-01-01T00:00:00Z")
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_ABOVE_MAX
    d2 = eval_leaf(Op.LT, T, cs, "1969-11-01T00:00:00Z")
    assert d2.verdict is Verdict.PRUNED
    assert d2.reasons[0].code == Reason.FILE_BELOW_MIN


def test_missing_stats_are_unknown_not_pruned():
    cs = fs_stat(None, None, nulls=None)
    d = eval_leaf(Op.GE, T, cs, "2024-03-05T00:00:00Z")
    assert d.verdict is Verdict.UNKNOWN
    assert d.reasons[0].code == Reason.FILE_STATS_MISSING.value


def test_truncated_max_is_not_a_sound_upper_bound():
    # stored max is only the prefix "abc" of the real max "abcxyz". There is
    # NO sound character padding: a literal "abcz" (>= the prefix but < a real
    # longer value) MUST keep the file as UNKNOWN/truncated, never be pruned
    # against the stored prefix.
    cs = fs_stat("aaaa", "abc", nulls=0, maxt=True)
    d = eval_leaf(Op.GE, N, cs, "abcz")
    assert d.verdict is Verdict.UNKNOWN
    assert d.reasons[0].code == Reason.FILE_STATS_TRUNCATED.value
    # A literal even above the (unsound) prefix can STILL not be pruned,
    # because the true max is unknown — over-pruning would drop rows.
    d2 = eval_leaf(Op.GE, N, cs, "abd")
    assert d2.verdict is Verdict.UNKNOWN
    assert d2.reasons[0].code == Reason.FILE_STATS_TRUNCATED.value
    # ... while a literal strictly below the sound MIN (min is intact) can
    # still prune LT using that usable side.
    d3 = eval_leaf(Op.LT, N, cs, "aa`")  # '`' (0x60) orders just below 'a'
    assert d3.verdict is Verdict.PRUNED
    assert d3.reasons[0].code == Reason.FILE_BELOW_MIN.value


def test_truncated_max_eq_literal_beyond_stored_is_kept():
    cs = fs_stat("aaaa", "abc", nulls=0, maxt=True)
    d = eval_leaf(Op.EQ, N, cs, "abcxyz")
    assert d.verdict is Verdict.UNKNOWN
    assert d.reasons[0].code == Reason.FILE_STATS_TRUNCATED.value


def test_is_null_positive_prune_needs_null_count():
    cs = fs_stat(TS("2024-01-01T00:00:00Z"), TS("2024-02-01T00:00:00Z"), nulls=0)
    d = eval_leaf(Op.IS_NULL, T, cs, negated=False)
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_NO_NULL.value


def test_is_null_unknown_when_null_count_missing():
    cs = fs_stat(None, None, nulls=None)
    d = eval_leaf(Op.IS_NULL, T, cs)
    assert d.verdict is Verdict.UNKNOWN
    assert d.reasons[0].code == Reason.FILE_NULL_COUNT_UNKNOWN.value


def test_is_not_null_prunes_all_null_file():
    cs = fs_stat(None, None, nulls=5)
    d = eval_leaf(Op.IS_NULL, T, cs, negated=True, num_rows=5)
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_ALL_NULL.value


def test_comparison_prunes_all_null_file():
    cs = fs_stat(None, None, nulls=3)
    d = eval_leaf(Op.GE, T, cs, "2024-01-01T00:00:00Z", num_rows=3)
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_ALL_NULL.value


def test_ne_prunes_single_valued_file():
    cs = fs_stat(7, 7, nulls=0)
    d = eval_leaf(Op.NE, A, cs, 7)
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_NE_ALL_EQUAL.value


def test_in_prunes_when_all_literals_outside():
    cs = fs_stat(1, 5, nulls=0)
    d = eval_leaf(Op.IN, A, cs, [100, 200])
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_IN_NO_MATCH.value


def test_between_disjoint_prunes():
    cs = fs_stat(TS("2024-03-10T00:00:00Z"), TS("2024-03-20T00:00:00Z"))
    d = eval_leaf(Op.BETWEEN, T, cs,
                  ["2024-03-01T00:00:00Z", "2024-03-05T00:00:00Z"])
    assert d.verdict is Verdict.PRUNED


def test_column_absent_is_unknown():
    f = FileStat(path="x", num_rows=1, size_bytes=1, row_groups=1,
                 stats={"ts": fs_stat()}, stats_version="colstats-v1",
                 pyarrow_version="20.0.0")
    leaf = Leaf(Op.GE, "missing")
    ctx = _ctx([])
    object.__setattr__(ctx, "columns", {**ctx.columns,
                                        "missing": Column("missing", V.INT)})
    from prune.kernel import _eval_tree_on_stats
    d = _eval_tree_on_stats(leaf, f, ctx)
    assert d.verdict is Verdict.UNKNOWN
    assert d.reasons[0].code == Reason.FILE_COLUMN_ABSENT.value


def test_stats_version_mismatch_is_unknown():
    f = FileStat(path="x", num_rows=1, size_bytes=1, row_groups=1,
                 stats={"amount": fs_stat(1, 2, 0)},
                 stats_version="colstats-v0", pyarrow_version="9.9.9")
    leaf = Leaf(Op.GE, "amount", value=1000)
    ctx = _ctx([])
    from prune.kernel import _eval_tree_on_stats
    d = _eval_tree_on_stats(leaf, f, ctx)
    assert d.verdict is Verdict.UNKNOWN
    assert d.reasons[0].code == Reason.FILE_STATS_VERSION_MISMATCH.value


# --------------------------------------------------------------------------- #
# Predicate-tree combination
# --------------------------------------------------------------------------- #

def test_and_prunes_if_one_branch_impossible():
    # ts in March AND amount > 1000; amount stats make conjunction impossible
    f = FileStat(path="x", num_rows=2, size_bytes=1, row_groups=1,
                 stats={"ts": fs_stat(TS("2024-03-02T00:00:00Z"),
                                      TS("2024-03-03T00:00:00Z")),
                        "amount": fs_stat(1, 2, 0)},
                 stats_version="colstats-v1", pyarrow_version="20.0.0")
    pred = parse_predicate({"op": "AND", "children": [
        {"op": "GE", "column": "ts", "value": "2024-03-01T00:00:00Z"},
        {"op": "GE", "column": "amount", "value": 1000}]})
    from prune.kernel import _eval_tree_on_stats
    d = _eval_tree_on_stats(pred, f, _ctx([]))
    assert d.verdict is Verdict.PRUNED
    assert d.reasons[0].code == Reason.FILE_ABOVE_MAX.value


def test_or_prunes_only_when_both_branches_impossible():
    f = FileStat(path="x", num_rows=2, size_bytes=1, row_groups=1,
                 stats={"amount": fs_stat(1, 2, 0)},
                 stats_version="colstats-v1", pyarrow_version="20.0.0")
    pred = parse_predicate({"op": "OR", "children": [
        {"op": "GE", "column": "amount", "value": 100},
        {"op": "LE", "column": "amount", "value": -10}]})
    from prune.kernel import _eval_tree_on_stats
    d = _eval_tree_on_stats(pred, f, _ctx([]))
    assert d.verdict is Verdict.PRUNED
    codes = {r.code for r in d.reasons}
    assert Reason.FILE_ABOVE_MAX.value in codes
    assert Reason.FILE_BELOW_MIN.value in codes


# --------------------------------------------------------------------------- #
# Partition level — candidates are transformed-value spans, never bucket ids
# --------------------------------------------------------------------------- #

def _ctx(parts, transform=None, versions_ok=True):
    return TableContext(
        name="t", columns={"ts": T, "name": N, "amount": A},
        transform=transform or MonthTransform("ts", "Asia/Shanghai"),
        partitions=parts,
        recorded_transform_version=(TRANSFORM_SPEC_VERSION if versions_ok else "other"),
        recorded_tzdb_version=(tzdb_version() if versions_ok else "other"))


def _part(label, files=()):
    return PartitionInfo(label=label, is_null=(label == "__null__"),
                         files=list(files))


def _f(path, nrows=1):
    return FileStat(path=path, num_rows=nrows, size_bytes=1, row_groups=1,
                    stats={}, stats_version="colstats-v1",
                    pyarrow_version="20.0.0")


def test_day_range_prunes_other_month_partitions():
    ctx = _ctx([_part("2024-02"), _part("2024-03"), _part("2024-04"),
                _part("__null__")])
    pred = parse_predicate({"op": "AND", "children": [
        {"op": "GE", "column": "ts", "value": "2024-03-05T00:00:00+08:00"},
        {"op": "LT", "column": "ts", "value": "2024-03-06T00:00:00+08:00"}]})
    plan = plan_prune(ctx, pred, "r")
    by = {p.label: p for p in plan.partitions}
    assert by["2024-03"].verdict == "KEPT"
    assert by["2024-02"].verdict == "PRUNED"
    assert by["2024-04"].verdict == "PRUNED"
    assert by["__null__"].verdict == "PRUNED"
    assert by["2024-02"].reason.code == Reason.PARTITION_OUTSIDE_CANDIDATES.value
    assert by["__null__"].reason.code == Reason.PARTITION_NULL_EXCLUDED.value
    assert plan.candidates["lo"] == "2024-03"
    assert plan.candidates["hi"] == "2024-03"


def test_negative_month_partition_candidates():
    ctx = _ctx([_part("1969-12"), _part("1970-01"), _part("2024-01")])
    pred = parse_predicate({"op": "AND", "children": [
        {"op": "GE", "column": "ts", "value": "1969-12-15T00:00:00+08:00"},
        {"op": "LT", "column": "ts", "value": "1969-12-16T00:00:00+08:00"}]})
    plan = plan_prune(ctx, pred, "r")
    by = {p.label: p for p in plan.partitions}
    assert by["1969-12"].verdict == "KEPT"
    assert by["1970-01"].verdict == "PRUNED"
    assert by["2024-01"].verdict == "PRUNED"


def test_is_null_only_keeps_null_partition():
    ctx = _ctx([_part("2024-03"), _part("__null__")])
    pred = parse_predicate({"op": "IS_NULL", "column": "ts"})
    plan = plan_prune(ctx, pred, "r")
    by = {p.label: p for p in plan.partitions}
    assert by["__null__"].verdict == "KEPT"
    assert by["2024-03"].verdict == "PRUNED"
    assert plan.candidates["null_bucket"] == "REQUIRED"


def test_is_not_null_prunes_null_partition():
    ctx = _ctx([_part("2024-03"), _part("__null__")])
    pred = parse_predicate({"op": "IS_NULL", "column": "ts", "negated": True})
    plan = plan_prune(ctx, pred, "r")
    by = {p.label: p for p in plan.partitions}
    assert by["__null__"].verdict == "PRUNED"
    assert by["2024-03"].verdict == "KEPT"


def test_predicate_on_non_partition_column_keeps_all_partitions():
    ctx = _ctx([_part("2024-03"), _part("__null__")])
    pred = parse_predicate({"op": "GE", "column": "amount", "value": 1})
    plan = plan_prune(ctx, pred, "r")
    assert all(p.verdict == "KEPT" for p in plan.partitions)
    assert plan.candidates["null_bucket"] == "POSSIBLE"


def test_version_mismatch_keeps_everything_and_reports_failure():
    ctx = _ctx([_part("2024-03"), _part("__null__")], versions_ok=False)
    pred = parse_predicate({"op": "IS_NULL", "column": "ts"})
    plan = plan_prune(ctx, pred, "r")
    assert all(p.verdict != "PRUNED" for p in plan.partitions)
    assert any(f["code"] == Reason.TRANSFORM_VERSION_MISMATCH.value
               for f in plan.failures)


def test_or_union_span_is_conservative_cover():
    # March day OR May day: hull [2024-03, 2024-05] keeps April (safe over-keep),
    # never drops a true candidate month.
    ctx = _ctx([_part(l) for l in ("2024-02", "2024-03", "2024-04", "2024-05")])
    pred = parse_predicate({"op": "OR", "children": [
        {"op": "AND", "children": [
            {"op": "GE", "column": "ts", "value": "2024-03-05T00:00:00+08:00"},
            {"op": "LT", "column": "ts", "value": "2024-03-06T00:00:00+08:00"}]},
        {"op": "AND", "children": [
            {"op": "GE", "column": "ts", "value": "2024-05-05T00:00:00+08:00"},
            {"op": "LT", "column": "ts", "value": "2024-05-06T00:00:00+08:00"}]}]})
    plan = plan_prune(ctx, pred, "r")
    by = {p.label: p for p in plan.partitions}
    assert by["2024-02"].verdict == "PRUNED"
    assert by["2024-03"].verdict == "KEPT"
    assert by["2024-04"].verdict == "KEPT"   # hull over-keep, must not be pruned
    assert by["2024-05"].verdict == "KEPT"
    assert plan.candidates["exact"] is False
