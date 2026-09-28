#!/usr/bin/env python3
"""End-to-end demonstration without HTTP: build → refresh → plan → validate.

Run:
    python3 scripts/example_demo.py

Prints, for several predicates, the two-level pruning amounts, the reason each
pruned file cannot match, and the independent full-scan zero-miss verdict.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prune.catalog import Catalog
from prune.config import TableSpec
from prune.kernel import Column, plan_prune
from prune.models import parse_predicate
from prune.reference import validate_plan_zero_miss
from prune.transforms import MonthTransform
from scripts.make_fixtures import build

COLUMNS = {
    "id": Column("id", "int"),
    "ts": Column("ts", "datetime"),
    "name": Column("name", "str"),
    "amount": Column("amount", "int"),
}

CASES = {
    "Shanghai single day 2024-03-05": {"op": "AND", "children": [
        {"op": "GE", "column": "ts", "value": "2024-03-05T00:00:00+08:00"},
        {"op": "LT", "column": "ts", "value": "2024-03-06T00:00:00+08:00"}]},
    "Negative-epoch December 1969 window": {"op": "BETWEEN", "column": "ts",
        "value": ["1969-12-10T00:00:00+08:00", "1969-12-31T23:59:59+08:00"]},
    "IS NULL ts": {"op": "IS_NULL", "column": "ts"},
    "Truncated-string equality": {"op": "EQ", "column": "name",
                                  "value": "z" * 60 + "002_tail_b"},
}


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="prune-demo-"))
    root = work / "events"
    build(root)
    spec = TableSpec("events", root, COLUMNS,
                     MonthTransform("ts", "Asia/Shanghai"), "__null__")
    with Catalog(work / "catalog.db") as cat:
        cat.refresh_table(spec)
        ctx = cat.load_context(spec, "events")

    domains = {n: c.type for n, c in COLUMNS.items()}
    for title, raw in CASES.items():
        predicate = parse_predicate(raw)
        plan = plan_prune(ctx, predicate, f"demo-{title[:6]}")
        allp, kept, pruned = [], [], []
        for p in plan.partitions:
            for f in p.files:
                allp.append(f.path)
                (kept if f.verdict != "PRUNED" else pruned).append(f.path)
        verdict = validate_plan_zero_miss(
            all_paths=allp, kept_paths=kept, pruned_paths=pruned,
            predicate=predicate, id_column="id", domains=domains)

        print(f"\n=== {title} ===")
        print("  candidates:", json.dumps(plan.candidates))
        print(f"  level 1: {plan.metrics['partitions_pruned']}/"
              f"{plan.metrics['partitions_total']} partitions pruned")
        print(f"  level 2: {plan.metrics['files_pruned']}/"
              f"{plan.metrics['files_total']} files, "
              f"{plan.metrics['rows_pruned']}/{plan.metrics['rows_total']} rows, "
              f"{plan.metrics['bytes_pruned']}/{plan.metrics['bytes_total']} bytes")
        for p in plan.partitions:
            for f in p.files:
                if f.verdict == "PRUNED":
                    why = f.reasons[0] if f.reasons else p.reason
                    print(f"    pruned {Path(f.path).name:24} "
                          f"[{why.code}] {why.detail[:70]}")
        for u in plan.uncertain:
            print(f"    kept?  {Path(u['target']).name:24} "
                  f"[{u['code']}] {u['detail'][:60]}")
        print(f"  zero-miss: ok={verdict['ok']} "
              f"matching_rows={verdict['expected_matching_rows']} "
              f"missed={verdict['missed_ids']} "
              f"category={verdict['failure_category']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
