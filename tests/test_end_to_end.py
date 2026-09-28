"""End-to-end correctness: the plan never drops a matching row.

For every query we run the real pipeline (catalog -> kernel) and an
INDEPENDENT reference (prune.reference scanning every file with
pyarrow.compute), then assert concrete outcomes: the exact set of surviving
files, pruning counts, reason categories, and zero missed ids. Randomized
queries fuzz the combination logic.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from prune.kernel import Reason, plan_prune
from prune.models import parse_predicate
from prune.reference import scan_matching_ids, validate_plan_zero_miss

SH = "+08:00"
DAY = ("2024-03-05T00:00:00+08:00", "2024-03-06T00:00:00+08:00")
NEG_DAY = ("1969-12-15T00:00:00+08:00", "1969-12-16T00:00:00+08:00")


def plan_paths(plan):
    kept, pruned = [], []
    for p in plan.partitions:
        for f in p.files:
            (kept if f.verdict != "PRUNED" else pruned).append(f.path)
    return kept, pruned


def run_case(ctx, all_paths, domains, predicate_obj):
    plan = plan_prune(ctx, predicate_obj, "test-req")
    kept, pruned = plan_paths(plan)
    verdict = validate_plan_zero_miss(
        all_paths=all_paths, kept_paths=kept, pruned_paths=pruned,
        predicate=predicate_obj, id_column="id", domains=domains)
    return plan, verdict


def test_month_partition_day_range_zero_miss(ctx, all_file_paths, domains):
    pred = parse_predicate({"op": "AND", "children": [
        {"op": "GE", "column": "ts", "value": DAY[0]},
        {"op": "LT", "column": "ts", "value": DAY[1]}]})
    plan, verdict = run_case(ctx, all_file_paths, domains, pred)

    assert verdict["ok"] is True
    assert verdict["failure_category"] is None
    assert verdict["expected_matching_rows"] == 2          # ids 3002, 3003

    kept = {f.path.split("/")[-1]
            for p in plan.partitions for f in p.files if f.verdict != "PRUNED"}
    assert kept == {"mar_early.parquet"}

    labels = {p.label: p.verdict for p in plan.partitions}
    assert labels["2024-03"] == "KEPT"
    assert labels["1969-12"] == "PRUNED"
    assert labels["2024-02"] == "PRUNED"
    assert labels["2024-04"] == "PRUNED"
    assert labels["2024-05"] == "PRUNED"
    assert labels["__null__"] == "PRUNED"
    assert plan.metrics["partitions_pruned"] == 5
    assert plan.metrics["files_pruned"] == 9
    assert plan.metrics["rows_pruned"] == 20
    # the file-level prune reasons are concrete range reasons
    reason_codes = {r.code for p in plan.partitions for f in p.files
                    for r in f.reasons}
    assert "FILE_BELOW_MIN" in reason_codes   # earlier files: max < range low
    # (partitions outside March are excluded at level 1 first)

    # expected ids computed independently, not by the kernel
    expected = scan_matching_ids(all_file_paths, pred, "id", domains)
    assert expected == {3002, 3003}


def test_negative_timestamp_day_zero_miss(ctx, all_file_paths, domains):
    pred = parse_predicate({"op": "AND", "children": [
        {"op": "GE", "column": "ts", "value": NEG_DAY[0]},
        {"op": "LT", "column": "ts", "value": NEG_DAY[1]}]})
    plan, verdict = run_case(ctx, all_file_paths, domains, pred)
    assert verdict["ok"] is True
    assert verdict["expected_matching_rows"] == 2          # ids 1001, 1002
    kept = {f.path.split("/")[-1]
            for p in plan.partitions for f in p.files if f.verdict != "PRUNED"}
    assert kept == {"neg_a.parquet", "neg_b.parquet"}      # neg_b spans month
    assert plan.metrics["partitions_pruned"] == 5
    expected = scan_matching_ids(all_file_paths, pred, "id", domains)
    assert expected == {1001, 1002}


def test_is_null_partition_and_file_level(ctx, all_file_paths, domains):
    pred = parse_predicate({"op": "IS_NULL", "column": "ts"})
    plan, verdict = run_case(ctx, all_file_paths, domains, pred)
    assert verdict["ok"] is True
    # All NULL timestamps are routed to the explicit null directory by the
    # writer: 9001,9002 (stats present) and 9011,9012 (no statistics block).
    expected = scan_matching_ids(all_file_paths, pred, "id", domains)
    assert expected == {9001, 9002, 9011, 9012}

    kept = {f.path.split("/")[-1]
            for p in plan.partitions for f in p.files if f.verdict != "PRUNED"}
    assert kept == {"null_ts.parquet", "no_stats.parquet"}

    verdicts = {f.path.split("/")[-1]: f.verdict
                for p in plan.partitions for f in p.files}
    # null_ts.parquet has null_count>0 => KEPT for IS NULL
    assert verdicts["null_ts.parquet"] == "KEPT"
    # no_stats file has no statistics block: null count unknown => UNKNOWN,
    # retained conservatively
    assert verdicts["no_stats.parquet"] == "UNKNOWN"
    uncertain = {u["target"].split("/")[-1]: u["code"] for u in plan.uncertain}
    assert uncertain["no_stats.parquet"] == "FILE_NULL_COUNT_UNKNOWN"
    # Partition-level: only the explicit null directory is a candidate.
    labels = {p.label: p.verdict for p in plan.partitions}
    assert labels["__null__"] == "KEPT"
    for real_month in ("1969-12", "2024-02", "2024-03", "2024-04", "2024-05"):
        assert labels[real_month] == "PRUNED"


def test_non_partition_column_all_null_prunes_at_file_level(ctx, all_file_paths, domains):
    # amount is all-NULL in the February file but partition predicates cannot
    # use it, so partitions are all kept; file stats must prune that file.
    pred = parse_predicate({"op": "GE", "column": "amount", "value": 0})
    plan, verdict = run_case(ctx, all_file_paths, domains, pred)
    assert verdict["ok"] is True
    feb = next(p for p in plan.partitions if p.label == "2024-02")
    f = feb.files[0]
    assert f.verdict == "PRUNED"
    assert f.reasons[0].code == "FILE_ALL_NULL"
    # no partition was eliminated (predicate is not on the partition column)
    assert plan.metrics["partitions_pruned"] == 0


def test_truncated_string_stats_keep_file_for_membership(ctx, all_file_paths, domains):
    pred = parse_predicate({"op": "EQ", "column": "name",
                            "value": "z" * 60 + "002_tail_b"})
    plan, verdict = run_case(ctx, all_file_paths, domains, pred)
    assert verdict["ok"] is True
    assert verdict["expected_matching_rows"] == 1          # id 5002
    may = next(p for p in plan.partitions if p.label == "2024-05")
    f = may.files[0]
    assert f.verdict == "UNKNOWN"
    assert f.reasons[0].code == "FILE_STATS_TRUNCATED"
    expected = scan_matching_ids(all_file_paths, pred, "id", domains)
    assert 5002 in expected


def test_truncated_string_never_prunes_against_prefix(ctx, all_file_paths, domains):
    # The May file's stored max is only a 60-char prefix of the real value.
    # No predicate that needs the UPPER bound may prune that file, even when
    # the literal sorts above the prefix: the true max is unknown.
    pred = parse_predicate({"op": "GE", "column": "name",
                            "value": "z" * 60 + "999Q"})
    plan, verdict = run_case(ctx, all_file_paths, domains, pred)
    assert verdict["ok"] is True
    may = next(p for p in plan.partitions if p.label == "2024-05")
    f = may.files[0]
    assert f.verdict == "UNKNOWN"
    assert f.reasons[0].code == "FILE_STATS_TRUNCATED"
    # the usable (intact) min still drives safe pruning in the other
    # direction via a different file; check an LT below every May value does
    # prune the May file using its sound minimum.
    below = parse_predicate({"op": "LT", "column": "name", "value": "a"})
    plan2, v2 = run_case(ctx, all_file_paths, domains, below)
    assert v2["ok"] is True
    may2 = next(p for p in plan2.partitions if p.label == "2024-05")
    assert may2.files[0].verdict == "PRUNED"
    assert may2.files[0].reasons[0].code == "FILE_BELOW_MIN"


def test_between_prunes_mid_march_file(ctx, all_file_paths, domains):
    pred = parse_predicate({"op": "BETWEEN", "column": "ts",
                            "value": ["2024-03-05T00:00:00+08:00",
                                      "2024-03-05T23:59:59+08:00"]})
    plan, verdict = run_case(ctx, all_file_paths, domains, pred)
    assert verdict["ok"] is True
    kept = {f.path.split("/")[-1]
            for p in plan.partitions for f in p.files if f.verdict != "PRUNED"}
    assert kept == {"mar_early.parquet"}


# --------------------------------------------------------------------------- #
# Randomized fuzzing: random predicates, compare against full scan
# --------------------------------------------------------------------------- #

TIMES = [
    "1969-12-10T00:00:00+08:00", "1969-12-15T12:00:00+08:00",
    "1970-01-01T00:30:00+08:00",
    "2024-02-15T00:00:00+08:00",
    "2024-03-04T22:00:00+08:00", "2024-03-05T08:00:00+08:00",
    "2024-03-10T08:00:00+08:00",
    "2024-04-01T00:30:00+08:00", "2024-04-20T00:00:00+08:00",
    "2024-05-02T08:00:00+08:00",
    "2025-06-01T00:00:00+08:00",
]
AMOUNTS = [-13, -10, 0, 20, 31, 33, 40, 50, 90, 100, 500]
NAMES = ["mar-target1", "neg-a1", "apr-1", "zz-end", "z" * 60 + "002_tail_b"]


def _random_leaf(rng):
    col = rng.choice(["ts", "amount", "name"])
    op = rng.choice(["GE", "LT", "EQ", "BETWEEN", "IN", "IS_NULL"])
    if col == "ts":
        vals = rng.sample(TIMES, 2)
    elif col == "amount":
        vals = [rng.choice(AMOUNTS), rng.choice(AMOUNTS)]
    else:
        vals = rng.sample(NAMES, 2)
    if op == "IS_NULL":
        return {"op": "IS_NULL", "column": col,
                "negated": rng.random() < 0.3}
    if op == "BETWEEN":
        lo, hi = sorted(vals)
        return {"op": "BETWEEN", "column": col, "value": [lo, hi]}
    if op == "IN":
        return {"op": "IN", "column": col,
                "value": rng.sample(vals, k=rng.randint(1, 2))}
    return {"op": op, "column": col, "value": vals[0]}


def _random_predicate(rng, depth=0):
    if depth >= 2 or rng.random() < 0.55:
        return _random_leaf(rng)
    op = rng.choice(["AND", "OR"])
    return {"op": op, "children": [_random_predicate(rng, depth + 1)
                                   for _ in range(rng.randint(2, 3))]}


@pytest.mark.parametrize("seed", range(40))
def test_random_predicates_never_miss_rows(seed, ctx, all_file_paths, domains):
    rng = random.Random(1000 + seed)
    pred_obj = parse_predicate(_random_predicate(rng))
    plan, verdict = run_case(ctx, all_file_paths, domains, pred_obj)
    assert verdict["ok"] is True, (
        f"seed={seed} category={verdict['failure_category']} "
        f"missed={verdict['missed_ids'][:5]}")


def test_plan_reports_request_id_and_versions(ctx, all_file_paths, domains):
    pred = parse_predicate({"op": "IS_NULL", "column": "ts"})
    plan = plan_prune(ctx, pred, "fixed-id-42")
    assert plan.request_id == "fixed-id-42"
    assert plan.transform_version == "month-tz-v1"
    assert plan.tzdb_version == "tzdata2024.2"
    assert plan.stats_schema_version == "colstats-v1"
