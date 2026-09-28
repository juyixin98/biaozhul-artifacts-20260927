"""小表穷举核验：被测搜索 vs 独立朴素预言机，逐组合 + 最优值都断言。

独立预言机 ``tests.reference_oracle`` 不导入被测内核：
* 它用自己的查表、分组、损失公式与无剪枝枚举；
* 本测试对小表逐层级组合比较两边的可行性、损失、类规模；
* 再对夹具中手工标注的期望值做具体数值断言（答案双来源：
  手算夹具 + 独立预言机，而非被测实现自证）。
"""

from __future__ import annotations

import itertools
import math

import pytest

from app.core.anonymization import evaluate, find_best_generalization
from app.core.logging_setup import StepLogger
from app.core.parsing import parse_dataset
from tests.conftest import load_fixture, make_payload
from tests.reference_oracle import (
    brute_force_optimum,
    levels_for,
    naive_classes,
    naive_loss,
    normalize_rows,
    sens_names,
)


def _search(fixture, k, l, settings):
    payload = make_payload(fixture, k, l)
    ds = parse_dataset(payload, settings)
    return ds, find_best_generalization(ds, StepLogger())


def test_tiny_table_optimum_concrete_values(settings):
    fx = load_fixture("tiny_patients")
    ds, res = _search(fx, 2, 2, settings)

    assert res.status == "succeeded"
    qi = [c.name for c in ds.qi_columns]
    got_levels = dict(zip(qi, res.best.levels))
    expected = fx["expected"]["k2_l2"]

    assert got_levels == expected["optimal_levels"] == {"age": 2, "city": 2}
    assert res.best.loss == pytest.approx(expected["info_loss"], abs=1e-6)
    assert (
        sorted((c.size for c in res.best.classes), reverse=True)
        == expected["class_sizes"]
        == [4, 2]
    )
    assert sorted((c.distinct_sensitive for c in res.best.classes), reverse=True) == [2, 2]


def test_every_level_combination_matches_independent_oracle(settings):
    """对全部 16 个组合，被测 evaluate 与独立朴素实现逐点一致。"""
    fx = load_fixture("tiny_patients")
    ds = parse_dataset(make_payload(fx, 2, 2), settings)

    qi = [c.name for c in ds.qi_columns]
    hiers = [c.hierarchy for c in ds.qi_columns]
    rows = normalize_rows(fx)
    lm = levels_for(fx["columns"])
    sens = sens_names(fx)

    for vec in itertools.product(*[range(h.height + 1) for h in hiers]):
        chosen = dict(zip(qi, vec))
        ev = evaluate(ds, tuple(vec))

        oclasses = naive_classes(rows, qi, sens, lm, chosen)
        osizes = sorted((c["size"] for c in oclasses), reverse=True)
        odistinct = sorted((c["distinct_sensitive"] for c in oclasses), reverse=True)

        assert sorted((c.size for c in ev.classes), reverse=True) == osizes
        assert sorted((c.distinct_sensitive for c in ev.classes), reverse=True) == odistinct
        assert ev.loss == pytest.approx(naive_loss(rows, qi, lm, chosen), abs=1e-12)
        assert ev.k_feasible == all(s >= 2 for s in osizes)
        assert ev.l_feasible == (
            all(s >= 2 for s in osizes) and all(d >= 2 for d in odistinct)
        )


def test_optimizer_matches_brute_force_optimum(settings):
    fx = load_fixture("tiny_patients")
    for k, l in [(2, 1), (2, 2), (3, 1), (4, 2), (5, 1)]:
        ds, res = _search(fx, k, l, settings)
        brute = brute_force_optimum(fx, k, l)

        qi = [c.name for c in ds.qi_columns]
        if brute.optimum is None:
            assert res.status != "succeeded", f"k={k},l={l} oracle says infeasible"
            continue

        assert res.status == "succeeded", f"k={k},l={l} oracle found a feasible point"
        got = dict(zip(qi, res.best.levels))
        assert got == brute.optimum["levels"]
        assert res.best.loss == pytest.approx(brute.optimum["loss"], abs=1e-12)


def test_explored_count_does_not_exceed_space_and_trace_has_optimum(settings):
    fx = load_fixture("tiny_patients")
    ds, res = _search(fx, 2, 2, settings)
    space = math.prod(c.hierarchy.height + 1 for c in ds.qi_columns)
    assert fx["expected"]["space_size"] == space == 9
    assert res.n_combinations_explored <= space
    # 小表顶层损失即最优，在找到更优前不存在可行剪枝，必须全枚举 9 个组合
    assert res.n_subtrees_pruned == 0
    assert res.n_combinations_explored == 9
    opt_events = [e for e in res.trace if e.get("event") == "optimum"]
    assert len(opt_events) == 1
    assert opt_events[0]["basis"].startswith("minimum information loss")


def test_pruning_preserves_optimum_on_larger_generated_table(settings):
    """生成更大的层级表：剪枝路径与无剪枝预言机给出相同最优。"""
    import random

    rng = random.Random(20260928)
    # 3 个 QI，每个 4 个层级；50 行随机合成
    levels = []
    raw_values = [f"v{i}" for i in range(8)]
    lev0 = {v: v for v in raw_values}
    lev1 = {v: f"g{int(v[1]) % 4}" for v in raw_values}
    lev2 = {v: f"g{int(v[1]) % 2}" for v in raw_values}
    lev3 = {v: "ALL" for v in raw_values}
    for name in ("q1", "q2", "q3"):
        levels.append(
            {
                "name": name,
                "role": "quasi_identifier",
                "hierarchy": {"levels": [dict(lev0), dict(lev1), dict(lev2), dict(lev3)]},
            }
        )
    sens_col = {"name": "s", "role": "sensitive"}
    rows = []
    for i in range(50):
        rows.append(
            {
                "q1": rng.choice(raw_values),
                "q2": rng.choice(raw_values),
                "q3": rng.choice(raw_values),
                "s": rng.choice(["a", "b", "c", "d"]),
            }
        )
    fixture = {"name": "gen", "columns": levels + [sens_col], "rows": rows}
    ds, res = _search(fixture, 2, 2, settings)
    brute = brute_force_optimum(fixture, 2, 2)
    qi = [c.name for c in ds.qi_columns]
    assert dict(zip(qi, res.best.levels)) == brute.optimum["levels"]
    assert res.best.loss == pytest.approx(brute.optimum["loss"], abs=1e-12)
    # 剪枝确实减少了评估（4^3=64）
    assert res.n_combinations_explored <= 64


def test_real_class_counts_are_preserved_not_estimated(settings):
    """信息损失优化后，返回的类规模之和必须等于真实行数，逐类计数可复核。"""
    fx = load_fixture("tiny_patients")
    ds, res = _search(fx, 2, 2, settings)
    assert sum(c.size for c in res.best.classes) == len(ds.rows) == 6
    # 每个成员下标唯一且覆盖所有行
    all_members = [i for c in res.best.classes for i in c.members]
    assert sorted(all_members) == list(range(6))
