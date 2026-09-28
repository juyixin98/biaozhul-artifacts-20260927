"""l-多样性：敏感值同质性导致 l 不可达，且与 k 不可达明确区分。"""

from __future__ import annotations

from tests.conftest import load_fixture
from tests.reference_oracle import brute_force_optimum, top_level_reachability
from tests.test_exhaustive_optimum import _search  # 复用搜索辅助

import pytest


def test_homogeneous_sensitive_attribute_l2_unreachable(settings):
    fx = load_fixture("homogeneous_dx")
    ds, res = _search(fx, 2, 2, settings)

    # 明确失败：不是成功，错误码具体
    assert res.status == "l_unreachable"
    assert res.failure_code == "L_UNREACHABLE"
    assert res.best is None

    # 独立预言机确认：顶层 k 可达但 l 不可达
    top = top_level_reachability(fx, 2, 2)
    assert top["k_reachable"] is True
    assert top["l_reachable"] is False
    assert top["below_l_distinct"] == [1, 1]

    # 返回的阻塞类只暴露计数，不含任何真实敏感值
    blockers = [c for c in res.top.classes if c.size >= 2 and c.distinct_sensitive < 2]
    assert len(blockers) == 2
    assert all(c.distinct_sensitive == 1 for c in blockers)
    # ClassInfo 内部成员下标不携带值；响应序列化后更是如此（在 API 测试再断言）


def test_same_table_k2_l1_succeeds_with_expected_optimum(settings):
    fx = load_fixture("homogeneous_dx")
    ds, res = _search(fx, 2, 1, settings)
    assert res.status == "succeeded"
    qi = [c.name for c in ds.qi_columns]
    expected = fx["expected"]["k2_l1"]
    assert dict(zip(qi, res.best.levels)) == expected["optimal_levels"] == {"age": 1}
    assert res.best.loss == pytest.approx(expected["info_loss"], abs=1e-6)
    assert sorted((c.size for c in res.best.classes), reverse=True) == [2, 2]

    brute = brute_force_optimum(fx, 2, 1)
    assert dict(zip(qi, res.best.levels)) == brute.optimum["levels"]


def test_l_diversity_counts_distinct_values_not_just_rows(settings):
    """2 行同类但敏感值相同 => distinct=1，不满足 l=2；敏感值不同则满足。"""
    fx = load_fixture("tiny_patients")
    ds, res = _search(fx, 2, 2, settings)
    # 最优 (2,2)：类规模 [4,2]，distinct [2,2]
    for c in res.best.classes:
        assert c.size >= 2 and c.distinct_sensitive >= 2
