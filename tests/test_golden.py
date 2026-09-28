"""Golden-set tests.

The expected verdicts/codes live in tests/golden/golden_cases.yaml — authored
independently of the kernel (the kernel never writes that file). Each case
asserts the exact verdict and exact finding-code sets.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sqlguard.core.kernel import Kernel
from sqlguard.core.policy import Policy

GOLDEN = Path(__file__).parent / "golden" / "golden_cases.yaml"


def _load_cases():
    data = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    return data["cases"]


CASES = _load_cases()


@pytest.fixture()
def kernel(policy: Policy, ro_fixture) -> Kernel:
    return Kernel(policy, ro_fixture)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(case: dict, kernel: Kernel):
    result = kernel.review(
        case.get("template", ""),
        params=case.get("params") or {},
        slots=case.get("slots") or {},
    )
    expected = case["verdict"]
    assert result.verdict.value == expected, (
        f"[{case['id']}] verdict mismatch: expected {expected}, "
        f"got {result.verdict.value}; "
        f"reject={result.reject_codes} "
        f"unanalyzable={result.unanalyzable_codes} "
        f"advisory={result.advisory_codes}"
    )
    for key, attr in (
        ("codes_reject", "reject_codes"),
        ("codes_unanalyzable", "unanalyzable_codes"),
        ("codes_advisory", "advisory_codes"),
    ):
        if key in case:
            assert sorted(getattr(result, attr)) == sorted(case[key]), (
                f"[{case['id']}] {key} mismatch: "
                f"expected {sorted(case[key])}, got {sorted(getattr(result, attr))}"
            )
    # every golden case must explain itself with at least one finding unless
    # it is a clean accept
    if expected != "accept":
        assert result.findings, f"[{case['id']}] non-accept verdict has no findings"
    # accepted cases should carry a rendered statement
    if expected == "accept":
        assert result.rendered_sql, f"[{case['id']}] accept without rendered SQL"


def test_golden_cases_are_independent_of_kernel():
    """Guard against the anti-pattern of the SUT generating its own oracles."""
    text = GOLDEN.read_text(encoding="utf-8")
    # kernel.py must never import/write the golden file
    kernel_src = (GOLDEN.parent.parent.parent / "sqlguard" / "core" / "kernel.py"
                  ).read_text(encoding="utf-8")
    assert "golden_cases" not in kernel_src
    assert text.strip().startswith("# Golden set")
    assert len(CASES) >= 30
