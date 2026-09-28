"""等价类与优化器：真实计数、k/l 判定、穷举最优与独立参考交叉核对。"""

from __future__ import annotations

import pytest

from anon_risk.kernel import equivalence as eq
from anon_risk.kernel import optimizer
from anon_risk.kernel.hierarchy import apply_vector, materialize
from anon_risk.kernel.parser import parse_dataset

from . import expected, oracle


@pytest.fixture(scope="module")
def tiny_prepared():
    payload = oracle.load_tiny_payload()
    ds = parse_dataset(payload)
    mats = {c: materialize(ds.hierarchies[c], [r[c] for r in ds.rows])
            for c in ds.qi_columns}
    return ds, mats


def test_oracle_self_check_loads_six_rows():
    rows = oracle.load_tiny_rows()
    assert len(rows) == 6
    assert {r.disease for r in rows} == {"Flu", "Cold"}


@pytest.mark.parametrize("vec,want",
                         list(expected.TINY_VECTOR_TABLE_K2_L2.items()))
def test_every_vector_matches_hand_table(tiny_prepared, vec, want):
    """12 个格点逐一核对：类大小分布、DM、k/l 判定都必须等于手算登记值。"""
    ds, mats = tiny_prepared
    levels = {"zip": vec[0], "age": vec[1]}
    keys = apply_vector(ds.rows, ds.qi_columns, mats, levels)
    report = eq.evaluate(ds, keys, levels, 2, 2)
    sizes = sorted((c.size for c in report.classes), reverse=True)
    assert sizes == want["sizes"]
    assert report.discernibility == want["dm"]
    assert report.k_ok is want["k_ok"]
    assert report.l_ok is want["l_ok"]


def test_brute_force_oracle_agrees_with_hand_table():
    """独立 oracle 的全量枚举必须复现手算表（抓登记错误，且不依赖被测代码）。"""
    rows = oracle.load_tiny_rows()
    for (zv, av), want in expected.TINY_VECTOR_TABLE_K2_L2.items():
        parts = oracle.class_partition(rows, zv, av)
        sizes = sorted((len(g) for g in parts.values()), reverse=True)
        assert sizes == want["sizes"], (zv, av)
        assert oracle.discernibility(parts) == want["dm"]
        assert oracle.k_ok(parts, 2) is want["k_ok"]
        assert oracle.l_ok(parts, 2) is want["l_ok"]


def test_optimizer_optimum_matches_independent_brute_force(tiny_prepared):
    ds, mats = tiny_prepared
    s = optimizer.suggest(ds, mats, 2, 2, combo_cap=10_000)

    # 独立 stdlib 暴力枚举的结论
    rows = oracle.load_tiny_rows()
    feasible, vec, dm, lm, feasible_rows = oracle.brute_force_optimum(rows, 2, 2)
    assert feasible is True
    assert tuple(s.levels_tuple) == vec
    assert s.discernibility == dm
    assert s.loss_metric == lm
    assert [tuple(v[:2]) for v in feasible_rows] == \
        expected.TINY_OPTIMUM_K2_L2["feasible_vectors"]

    # 手工期望值
    assert s.status == "FEASIBLE"
    assert s.levels == expected.TINY_OPTIMUM_K2_L2["levels"]
    assert s.class_sizes == expected.TINY_OPTIMUM_K2_L2["class_sizes"]
    assert s.discernibility == expected.TINY_OPTIMUM_K2_L2["discernibility"]
    assert s.loss_metric == expected.TINY_OPTIMUM_K2_L2["loss_metric"]
    assert s.evaluated_vectors == 12
    assert s.total_vectors == 12


def test_real_class_counts_are_used_not_uniform_assumption(tiny_prepared):
    """反均匀假设：在 (3,1) 下真实类大小是 [4,2]，DM=20 而非均匀的 18。

    优化器必须因此偏好 (1,2)（DM=18），证明它读的是真实计数。
    """
    ds, mats = tiny_prepared
    keys = apply_vector(ds.rows, ds.qi_columns, mats, {"zip": 3, "age": 1})
    report = eq.evaluate(ds, keys, {"zip": 3, "age": 1}, 2, 2)
    sizes = sorted((c.size for c in report.classes), reverse=True)
    assert sizes == [4, 2]
    assert report.discernibility == 4 * 4 + 2 * 2
    s = optimizer.suggest(ds, mats, 2, 2)
    assert s.discernibility < report.discernibility
    assert tuple(s.levels_tuple) == (1, 2)


def test_l_diversity_null_is_not_counted_as_sensitive_value():
    """敏感列 NULL 不允许被当成一种真实敏感值去凑 l。"""
    payload = {
        "columns": ["z", "s"],
        "quasi_identifiers": ["z"],
        "sensitive": ["s"],
        "rows": [["a", "Flu"], ["a", None], ["b", "Cold"], ["b", None]],
        "hierarchies": {"z": {"levels": []}},
    }
    ds = parse_dataset(payload)
    mats = {c: materialize(ds.hierarchies[c], [r[c] for r in ds.rows])
            for c in ds.qi_columns}
    keys = apply_vector(ds.rows, ["z"], mats, {"z": 0})
    report = eq.evaluate(ds, keys, {"z": 0}, 2, 2)
    # 每类 2 人但敏感值只有 1 个非 NULL => l=2 不达标
    assert report.k_ok is True
    assert report.l_ok is False
    assert report.sensitive_nulls_total == 2
    cls = report.classes[0]
    assert cls.sensitive_distinct == 1
    assert cls.sensitive_nulls == 1


def test_qi_null_rows_remain_in_sample_and_class():
    """QI 为 NULL 的行不被移出样本：自成类（不与非 NULL 合并），单独计数。"""
    payload = {
        "columns": ["z", "s"],
        "quasi_identifiers": ["z"],
        "sensitive": ["s"],
        "rows": [[None, "Flu"], [None, "Cold"], ["a", "Flu"], ["a", "Cold"]],
        "hierarchies": {"z": {"levels": [
            {"rule": "map", "mapping": {"a": "ALL"}}]}},
    }
    ds = parse_dataset(payload)
    mats = {c: materialize(ds.hierarchies[c], [r[c] for r in ds.rows])
            for c in ds.qi_columns}
    keys = apply_vector(ds.rows, ["z"], mats, {"z": 1})
    report = eq.evaluate(ds, keys, {"z": 1}, 2, 2)
    assert report.row_count == 4               # 行没被丢
    assert report.null_q_rows == 2             # NULL QI 行仍在
    null_cls = [c for c in report.classes if c.qi_has_null]
    assert len(null_cls) == 1
    assert null_cls[0].size == 2               # 两个 NULL 彼此同类
    assert ("a",) not in [c.qi_key for c in null_cls]


def test_threshold_unreachable_when_l_exceeds_distinct_values(tiny_prepared):
    ds, mats = tiny_prepared
    s = optimizer.suggest(ds, mats, 2, 3)
    assert s.status == "UNREACHABLE"
    assert s.feasible is False
    ev = s.unreachable_evidence
    assert ev["full_generalization_l_ok"] is False
    assert ev["distinct_non_null_sensitive_values"] == 2


def test_threshold_unreachable_when_k_exceeds_n(tiny_prepared):
    ds, mats = tiny_prepared
    s = optimizer.suggest(ds, mats, 7, 1)
    assert s.status == "UNREACHABLE"
    ev = s.unreachable_evidence
    assert ev["row_count"] == 6
    assert ev["full_generalization_k_ok"] is False
    assert any("lt_k_7" in r for r in s.verdict_basis)


def test_lattice_cap_refuses_with_specific_category(tiny_prepared):
    ds, mats = tiny_prepared
    with pytest.raises(Exception) as ei:
        optimizer.suggest(ds, mats, 2, 2, combo_cap=3)
    assert ei.value.code.value == "LATTICE_TOO_LARGE"
    assert ei.value.details["total_vectors"] == 12


def test_report_carries_disclaimer_and_metric_version(tiny_prepared):
    ds, mats = tiny_prepared
    keys = apply_vector(ds.rows, ds.qi_columns, mats, {"zip": 0, "age": 0})
    report = eq.evaluate(ds, keys, {"zip": 0, "age": 0}, 2, 2,
                         metric_version="1.0.0")
    assert "不构成完整隐私保证" in report.disclaimer
    assert report.metric_version == "1.0.0"


def test_tiny_group_classified_high_with_reasons(tiny_prepared):
    """极小群体（单例）必须明确为 HIGH 且给出机器可读判定原因。"""
    ds, mats = tiny_prepared
    keys = apply_vector(ds.rows, ds.qi_columns, mats, {"zip": 0, "age": 0})
    report = eq.evaluate(ds, keys, {"zip": 0, "age": 0}, 2, 2)
    assert all(c.risk == "HIGH" for c in report.classes)
    for c in report.classes:
        assert any(r.startswith("class_size_1_lt_k_2") for r in c.reasons)
        assert any(r.startswith("distinct_non_null_1_lt_l_2") for r in c.reasons)
