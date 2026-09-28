"""缺失值/极小群体：NULL 参与计数、独立成组，最优结果与预言机一致。"""

from __future__ import annotations

import pytest

from tests.conftest import load_fixture
from tests.reference_oracle import brute_force_optimum, top_level_reachability
from tests.test_exhaustive_optimum import _search


def test_null_rows_remain_and_form_group(settings):
    fx = load_fixture("null_patients")
    ds, res = _search(fx, 2, 2, settings)
    expected = fx["expected"]["k2_l2"]

    assert res.status == "succeeded"
    qi = [c.name for c in ds.qi_columns]
    assert dict(zip(qi, res.best.levels)) == expected["optimal_levels"]
    assert dict(zip(qi, res.best.levels)) == {"age": 2, "city": 2}
    assert res.best.loss == pytest.approx(expected["info_loss"], abs=1e-6)

    sizes = sorted((c.size for c in res.best.classes), reverse=True)
    assert sizes == expected["class_sizes"] == [3, 2, 2]
    assert len(ds.rows) == fx["expected"]["n_rows"] == 7

    # NULL 类：2 行，2 个不同敏感值，且被标记 contains_null_qi
    null_classes = [c for c in res.best.classes if c.contains_null_qi]
    assert len(null_classes) == 1
    nc = null_classes[0]
    assert nc.size == expected["null_class_size"] == 2
    assert nc.distinct_sensitive == expected["null_class_distinct_sensitive"] == 2


def test_null_optimum_matches_independent_brute_force(settings):
    fx = load_fixture("null_patients")
    ds, res = _search(fx, 2, 2, settings)
    brute = brute_force_optimum(fx, 2, 2)
    qi = [c.name for c in ds.qi_columns]
    assert dict(zip(qi, res.best.levels)) == brute.optimum["levels"]
    assert res.best.loss == pytest.approx(brute.optimum["loss"], abs=1e-12)


def test_null_class_is_identifiability_risk_when_singleton(settings):
    """单条 NULL 行在任何层级都是 size=1 的高危类（NULL 不与真实值合并）。"""
    fx = load_fixture("null_patients")
    # 复制夹具但只保留一条 NULL 行
    one_null = {
        **fx,
        "rows": [r for r in fx["rows"] if r["age"] is not None][:5]
        + [r for r in fx["rows"] if r["age"] is None][:1],
    }
    top = top_level_reachability(one_null, 2, 1)
    # 6 行里 1 行 NULL，顶层 NULL 独立成组 size=1 => k 不可达
    assert top["k_reachable"] is False
    assert 1 in top["below_k_sizes"]
