#!/usr/bin/env python3
"""Aggregate per-suite test logs from one run directory into summary.json.

The four integration suites each write <suite>.json containing per-case
judgements with intermediate state. This merges them, checks consistency
against the fixture manifest, and writes a single replay document.
"""

import glob
import json
import os
import sys

SUITES = ["oracle_crosscheck", "store_contract", "resource_exhaustion", "service_api"]


def main(run_dir: str) -> int:
    totals = {"total": 0, "pass": 0, "fail": 0, "skip": 0}
    suites = {}
    all_cases = []
    run_numbers = set()
    for name in SUITES:
        suite_files = sorted(glob.glob(os.path.join(run_dir, name, "*.json")))
        if not suite_files:
            suites[name] = {"total": 0, "pass": 0, "fail": 0, "skip": 0, "missing": True}
            continue
        counts = {"total": 0, "pass": 0, "fail": 0, "skip": 0}
        for path in suite_files:
            doc = json.load(open(path))
            run_numbers.add(doc.get("run_number", 0))
            for k in counts:
                counts[k] += doc["counts"][k]
            for case in doc["cases"]:
                case = dict(case)
                case["suite"] = name
                case["test_name"] = doc.get("test_name")
                all_cases.append(case)
        for k in totals:
            totals[k] += counts[k]
        suites[name] = counts

    # Fixture coverage cross-check: every good/bad case declared by the generator
    # must appear as a PASS (good) or a category-agreed PASS (bad) in the oracle suite.
    manifest_path = os.path.join(
        os.path.dirname(__file__), "..", "tests", "fixtures", "fixtures_manifest.json"
    )
    coverage = {"fixture_cases": 0, "exercised": 0, "missing_case_names": []}
    if os.path.exists(manifest_path):
        manifest = json.load(open(manifest_path))
        names = {c["name"] for c in manifest["cases"]}
        coverage["fixture_cases"] = len(names)
        judged = {c["case"] for c in all_cases if c["suite"] == "oracle_crosscheck"}
        # Some manifest cases are folded into parameterised tests; a missing name is
        # surfaced for audit, not treated as a hard failure here (cargo result is).
        coverage["missing_case_names"] = sorted(names - judged)
        coverage["exercised"] = len(names) - len(coverage["missing_case_names"])

    failures = [
        {"suite": c["suite"], "case": c["case"], "reason": c["reason"]}
        for c in all_cases
        if c["judgement"] == "FAIL"
    ]

    summary = {
        "run_id": os.path.basename(run_dir.rstrip(os.sep)),
        "run_number": max(run_numbers) if run_numbers else 0,
        "suites": suites,
        "counts": totals,
        "fixture_coverage": coverage,
        "failures": failures,
        "cases": all_cases,
    }
    with open(os.path.join(run_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
