"""Inspect a replay run by number (or the latest run).

Usage::

    .venv/bin/python scripts/inspect_runs.py            # latest run summary
    .venv/bin/python scripts/inspect_runs.py 3          # run number 3
    .venv/bin/python scripts/inspect_runs.py 3 --checks # show CHECK events
    .venv/bin/python scripts/inspect_runs.py 3 --grep zw # filter tests/checks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_ROOT = ROOT / "test-runs"


def _resolve(number: int | None) -> Path:
    dirs = sorted(p for p in RUN_ROOT.glob("run-*") if p.is_dir())
    if not dirs:
        sys.exit("no test runs found; run scripts/verify.sh first")
    if number is None:
        return dirs[-1]
    prefix = f"run-{number:05d}-"
    for d in dirs:
        if d.name.startswith(prefix):
            return d
    sys.exit(f"no run numbered {number}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("number", type=int, nargs="?")
    ap.add_argument("--checks", action="store_true", help="print CHECK events")
    ap.add_argument("--grep", default=None)
    args = ap.parse_args()

    d = _resolve(args.number)
    summary = json.loads((d / "summary.json").read_text())
    print(f"run {summary['run_number']}: {d.name}")
    print(f"passed={summary['passed']} failed={summary['failed']} "
          f"skipped={summary['skipped']}")
    if summary["failed_tests"]:
        print("\nFAILURES:")
        for t in summary["failed_tests"]:
            print("  -", t)
    if summary["skipped_tests"]:
        print("\nSKIPPED:")
        for t in summary["skipped_tests"]:
            print("  -", t)

    if args.checks:
        print("\nCHECK events:")
        for line in (d / "events.jsonl").read_text().splitlines():
            ev = json.loads(line)
            if ev.get("event") != "CHECK":
                continue
            if args.grep and args.grep not in ev.get("test", "") and \
               args.grep not in ev.get("check", ""):
                continue
            verdict = "ok " if ev["passed"] else "XX "
            print(f"  [{verdict}] {ev['test']} :: {ev['check']}")
            print(f"         reason: {ev['reason']}")
            if not ev["passed"]:
                print(f"         expected: {ev['expected']}")
                print(f"         actual:   {ev['actual']}")


if __name__ == "__main__":
    main()
