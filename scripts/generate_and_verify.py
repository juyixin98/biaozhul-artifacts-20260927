#!/usr/bin/env python3
"""Generate a cross-page nested-list dataset and run the full verification.

Demonstrates the page-boundary contract directly (no server needed):
the kernel paginates its own D/R slot stream, writes a PyArrow Parquet file
with a small data-page size, and fastparquet confirms every external page
starts at repetition level 0 and that the D/R streams agree.

Usage:
    python scripts/generate_and_verify.py [--records N] [--page-bytes B]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.schema import Schema  # noqa: E402
from app.core.verifier import verify  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, default=2000)
    ap.add_argument("--page-bytes", type=int, default=128)
    ap.add_argument("--seed", type=int, default=20260928)
    args = ap.parse_args()

    random.seed(args.seed)
    schema = Schema({
        "name": "root", "type": "struct",
        "children": [
            {"name": "id", "type": "int64", "nullable": False},
            {"name": "matrix", "type": "list",
             "item": {"name": "element", "type": "list",
                       "item": {"name": "element", "type": "int32"}}},
            {"name": "flags", "type": "list",
             "item": {"name": "element", "type": "struct", "children": [
                 {"name": "tag", "type": "string"},
                 {"name": "on", "type": "boolean"},
             ]}},
        ],
    })

    def record(i: int) -> dict:
        matrix = []
        for _ in range(random.randint(0, 5)):
            if random.random() < 0.15:
                matrix.append(None)
            elif random.random() < 0.15:
                matrix.append([])
            else:
                matrix.append([
                    random.choice([None, random.randint(-50, 50)])
                    for _ in range(random.randint(0, 5))
                ])
        return {
            "id": i,
            "matrix": matrix,
            "flags": [
                {"tag": random.choice(["a", "b", None]),
                 "on": random.choice([True, False, None])}
                for _ in range(random.randint(0, 4))
            ],
        }

    records = [record(i) for i in range(args.records)]
    with tempfile.TemporaryDirectory(prefix="pv-demo-") as work:
        report = verify(
            schema, records,
            workdir=Path(work),
            page_slot_target=20,
            parquet_page_bytes=args.page_bytes,
            expected=records,
        )

    print("status:", report["status"])
    for step in report["steps"]:
        print(f"  step {step['step']}: {step['status']}")
    if report["findings"]:
        print("findings:")
        for f in report["findings"]:
            print(f"  [{f['severity']}] {f['code']}: {f['message']}")
    print("external pages per leaf:",
          json.dumps(report["oracle_page_count"]))


if __name__ == "__main__":
    main()
