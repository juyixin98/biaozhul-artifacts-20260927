"""阈值不可达：最粗泛化仍有小类/敏感同质时明确报失败类别，不伪装成功。"""

from __future__ import annotations

from app.core.errors import CATEGORY, FailureCode
from tests.conftest import load_fixture
from tests.reference_oracle import top_level_reachability
from tests.test_exhaustive_optimum import _search


def test_k_unreachable_returns_explicit_failure(settings):
    fx = load_fixture("unique_signatures")
    ds, res = _search(fx, 2, 1, settings)

    assert res.status == "k_unreachable"
    assert res.failure_code == "K_UNREACHABLE"
    assert CATEGORY[FailureCode.K_UNREACHABLE] == "threshold_unreachable"
    assert res.best is None

    # 具体证据：顶层 (1,1) 下唯一行的类规模为 1
    sizes = sorted((c.size for c in res.top.classes), reverse=True)
    assert sizes == fx["expected"]["k2_l1"]["top_class_sizes"] == [2, 2, 1]

    blockers = [c for c in res.top.classes if c.size < 2]
    assert [c.size for c in blockers] == [1]

    # 独立预言机一致
    top = top_level_reachability(fx, 2, 1)
    assert top["k_reachable"] is False
    assert top["below_k_sizes"] == [1]


def test_k_unreachable_message_names_threshold(settings):
    fx = load_fixture("unique_signatures")
    _, res = _search(fx, 2, 1, settings)
    assert "k=2 unreachable" in res.message


def test_trace_records_unreachable_with_basis(settings):
    fx = load_fixture("unique_signatures")
    _, res = _search(fx, 2, 1, settings)
    events = [e for e in res.trace if e.get("event") == "unreachable"]
    assert len(events) == 1
    assert events[0]["code"] == "K_UNREACHABLE"
    assert events[0]["basis"].startswith("top-level evaluation")


def test_k_unreachable_never_reports_success_metrics(settings):
    fx = load_fixture("unique_signatures")
    _, res = _search(fx, 2, 1, settings)
    assert res.status != "succeeded"
    assert res.best is None
