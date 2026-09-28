"""Golden-set test: hand-authored expectations in golden/cases.json.

The golden file is *not* produced by the analyzer; it encodes the review
team's intended accept/reject/unanalyzable decision for each boundary case.
This harness checks:

1. the kernel's verdict equals the expected verdict;
2. every expected reason code is present at the expected severity;
3. (where declared) inert placeholder occurrences were recognized;
4. the case inventory itself is balanced (it tests both injection
   true-positives and safe true-negatives), guarding against a golden file
   that only contains easy accepts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "golden" / "cases.json").read_text("utf-8"))
CASES = GOLDEN["cases"]


def test_golden_inventory_is_balanced():
    groups = {c["group"] for c in CASES}
    assert "true_positive_injection" in groups
    assert "true_negative_safe" in groups
    assert "false_positive_guards" in groups
    # at least as many injection-positive cases as accepts, and both verdicts
    assert sum(c["expect_verdict"] == "reject"
               or c["expect_verdict"] == "unanalyzable" for c in CASES) >= 15
    assert sum(c["expect_verdict"] == "accept" for c in CASES) >= 10


def test_golden_ids_are_unique_and_have_sources():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    for c in CASES:
        assert c["title"] and c["sql"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(kernel, case):
    result = kernel.review(
        case["sql"],
        parameters=case.get("parameters", {}),
        identifiers=case.get("identifiers", {}),
    )
    errors = [f.code for f in result.findings if f.severity == "error"]
    warnings = [f.code for f in result.findings if f.severity == "warning"]

    assert result.verdict == case["expect_verdict"], (
        f"{case['id']} ({case['title']}): verdict {result.verdict} != "
        f"{case['expect_verdict']}; findings={errors + warnings}"
    )
    for code in case["expect_codes"]:
        # BINDING_UNUSED is a warning; others must be hard errors
        bucket = warnings if code == "BINDING_UNUSED" else errors
        assert code in bucket, (
            f"{case['id']}: expected code {code}, got errors={errors} "
            f"warnings={warnings}"
        )
    if case["expect_codes"] == [] and case["expect_verdict"] == "accept":
        assert errors == [], f"{case['id']}: unexpected errors {errors}"

    if "expect_inert" in case:
        inert_texts = [o["text"] for o in result.inert_occurrences]
        for needle in case["expect_inert"]:
            assert needle in inert_texts, (
                f"{case['id']}: expected inert occurrence {needle!r}, "
                f"saw {inert_texts}"
            )
        # inert occurrences must never be treated as bound parameters
        for occ in case["expect_inert"]:
            assert not any(
                str(p.get("ref")) == occ.lstrip(":@$?{}")
                and p.get("position") not in ("table(rejected)",
                                              "order_by(rejected)")
                for p in result.bound_parameters
            )
