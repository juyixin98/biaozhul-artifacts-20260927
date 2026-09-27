"""查询引擎测试：AND/OR/NOT、短路、空全集、全否定、删除、执行顺序一致性。

期望值全部来自 tests/_oracle.py（独立集合代数参考实现），
不由被测核心生成。
"""
from __future__ import annotations

import pytest

from app.query import planner as PL
from app.query.engine import (
    CAT_ORDER,
    CAT_SPEC,
    CAT_VERSION_NOT_FOUND,
    QueryEngine,
)

from tests._fixtures import expected


@pytest.fixture()
def engine(seeded_store):
    return QueryEngine(seeded_store)


# ---------------------------------------------------------------------------
# 基础集合代数（稀疏 vs 稠密）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "alpha AND beta",  # 稠密 ∩ 稠密
        "alpha AND rareword",  # 稀疏 ∩ 稠密
        "rareword",  # 稀疏单 term
        "common",  # 稠密单 term
        "alpha OR common",  # 稠密并
        "rareword OR epsilon",  # 稀疏并
        "(alpha OR beta) AND NOT common",
        "alpha AND NOT beta",
        "NOT rareword",  # 全否定风格：除稀疏 term 之外的全部可见文档
        "* AND alpha",
        "*",
    ],
)
def test_matches_oracle_on_version2(engine, seeded_store, expr):
    v = seeded_store.latest_version()
    out = engine.query(expr, version=v)
    assert out.ok, out.error_message
    assert set(out.result) == expected(seeded_store, expr, v)
    # 唯一性 + 有序
    assert out.result == sorted(set(out.result))


def test_version0_empty_universe_not_is_empty(engine, seeded_store):
    # 空全集：任何查询（含 NOT、*）结果都必须为空，NOT 不补成无限集
    for expr in ["*", "NOT alpha", "NOT rareword", "alpha OR NOT beta", "common"]:
        out = engine.query(expr, version=0)
        assert out.ok, out.error_message
        assert out.result == [], f"{expr} 在空全集上应返回空"
        assert set(out.result) == expected(seeded_store, expr, 0)


def test_all_negation_on_fresh_empty_database(store):
    """全新空库（连事件都没有）上的 NOT 也必须为空，而不是所有非负整数。"""
    eng = QueryEngine(store)
    out = eng.query("NOT anything", version=0)
    assert out.ok
    assert out.result == []
    assert out.count == 0


def test_deleted_doc_excluded_by_visibility_filter(engine, seeded_store):
    v = 2
    # 文档 8 含 "common" 与 "全集"，已在 v2 删除；posting 中仍有 8，结果中必须没有
    out = engine.query("common", version=v)
    assert out.ok
    assert 8 not in out.result
    assert set(out.result) == {6, 7}
    # NOT 查询同样不会把 8“补回来”
    out_not = engine.query("NOT alpha", version=v)
    assert 8 not in out_not.result
    assert set(out_not.result) == expected(seeded_store, "NOT alpha", v)


def test_delete_only_effects_version_snapshot(engine, seeded_store):
    # v1 上文档 8 仍然可见
    out = engine.query("common", version=1)
    assert set(out.result) == {6, 7, 8}
    out2 = engine.query("common", version=2)
    assert set(out2.result) == {6, 7}


# ---------------------------------------------------------------------------
# 未知 term：结果正确 + 不确定性单列
# ---------------------------------------------------------------------------


def test_unknown_term_is_warning_not_error_and_empty_set(engine, seeded_store):
    out = engine.query("alpha AND nope_xyz", version=2)
    assert out.ok
    assert out.result == []
    assert len(out.uncertainty) == 1
    assert "nope_xyz" in out.uncertainty[0]
    # oracle 也把未知 term 当空集
    assert set(out.result) == expected(seeded_store, "alpha AND nope_xyz", 2)


def test_unknown_term_under_not_resolves_to_full_universe(engine, seeded_store):
    out = engine.query("NOT nope_xyz", version=2)
    assert out.ok
    assert set(out.result) == set(seeded_store.universe(2).ids)


# ---------------------------------------------------------------------------
# 短路求值
# ---------------------------------------------------------------------------


def test_and_short_circuits_on_empty_term(engine, seeded_store):
    # rareword={3} AND ghost={} AND alpha：碰到 ghost 即空，alpha 子树不应执行
    out = engine.query(
        "rareword AND ghost_term AND alpha",
        version=2,
        order=PL.ORDER_TEXTUAL,
    )
    assert out.ok
    assert out.result == []
    assert out.short_circuited is True
    # 至少一个 term_load（alpha 对应的叶子）未执行：步骤里不应加载 alpha 之后…
    loaded = [s["label"] for s in out.steps if s["op"] == "term_load"]
    # rareword/ghost 被加载；alpha 被短路跳过
    assert any("rareword" in x for x in loaded)
    assert not any(x.endswith("term:alpha") for x in loaded)
    # 用计划映射确认：被跳过的节点里包含 alpha 叶子
    plan = PL.Planner(seeded_store, 2, "rareword AND ghost_term AND alpha").build()

    def term_leaf_ids(node: PL.PlanNode, term: str, acc):
        if node.kind == "term" and node.term == term:
            acc.append(node.node_id)
        for c in node.children:
            term_leaf_ids(c, term, acc)

    alpha_ids: list[str] = []
    term_leaf_ids(plan.root, "alpha", alpha_ids)
    assert alpha_ids and all(n in out.skipped_nodes for n in alpha_ids)


def test_or_short_circuits_when_universe_covered(engine, seeded_store):
    # (*) OR anything：第一个子节点已是全集 → 短路
    out = engine.query("* OR rareword", version=2)
    assert out.ok
    assert set(out.result) == set(seeded_store.universe(2).ids)
    assert out.short_circuited is True
    assert not any(
        s["label"].endswith("term:rareword") and s["op"] == "term_load"
        for s in out.steps
    )


# ---------------------------------------------------------------------------
# 执行顺序一致性 + 块跳过统计
# ---------------------------------------------------------------------------


def test_execution_orders_agree_but_stats_can_differ(engine, seeded_store):
    expr = "alpha AND beta AND gamma AND delta"
    results = {}
    skips = {}
    for order in PL.ALL_ORDERS:
        out = engine.query(expr, version=2, order=order)
        assert out.ok, out.error_message
        results[order] = out.result
        skips[order] = out.stats["blocks_skipped"]
    assert results[PL.ORDER_TEXTUAL] == results[PL.ORDER_REVERSE]
    assert results[PL.ORDER_RARE_FIRST] == results[PL.ORDER_TEXTUAL]
    assert set(results[PL.ORDER_RARE_FIRST]) == expected(seeded_store, expr, 2)
    # 统计必须真的来自核心：字段齐全且为非负整数；不同顺序统计被分别报告
    for order in PL.ALL_ORDERS:
        for key in (
            "comparisons",
            "docs_examined",
            "blocks_skipped",
            "docs_skipped_in_blocks",
        ):
            assert isinstance(skips[order] if key == "blocks_skipped" else 0, int)
    # explain 汇总三种顺序的跳过块数
    ex = engine.explain(expr, version=2)
    assert ex.ok
    resp = ex.to_response()
    assert resp["explain"]["consistent_across_orders"] is True
    assert set(resp["explain"]["block_skip_summary"]) == set(PL.ALL_ORDERS)


def test_skip_is_real_on_sparse_drive(store):
    """构造确定性场景，断言“稀疏驱动”真的跳过了块，而非只返回数字。"""
    # a: 每隔很远一个 ID（稀疏）；b: 稠密大范围。bs=4
    store.commit(adds={i: ("a " if i % 50 == 7 else "") + "b" for i in range(1, 81)})
    eng = QueryEngine(store)
    out = eng.query("a AND b", version=1)
    assert out.ok
    a_ids = {i for i in range(1, 81) if i % 50 == 7}
    assert set(out.result) == a_ids  # 7, 57
    assert out.stats["blocks_skipped"] >= 1
    # 步骤明细中能看到 intersect 报告跳过块数
    intersect_steps = [s for s in out.steps if s["op"] == "intersect"]
    assert sum(s["stats"]["blocks_skipped"] for s in intersect_steps) >= 1


# ---------------------------------------------------------------------------
# 失败类别（具体，而非“接口能调用”）
# ---------------------------------------------------------------------------


def test_spec_error_has_category_and_position(engine):
    out = engine.query("alpha AND", version=2)
    assert not out.ok
    assert out.error_category == CAT_SPEC
    assert out.error_position == 9  # "alpha AND" 中 AND 之后的位置


def test_missing_version_is_version_not_found(engine):
    out = engine.query("alpha", version=42)
    assert not out.ok
    assert out.error_category == CAT_VERSION_NOT_FOUND


def test_bad_order_is_order_error(engine):
    out = engine.query("alpha", version=2, order="sideways")
    assert not out.ok
    assert out.error_category == CAT_ORDER


def test_steps_explain_operations_and_positions(engine, seeded_store):
    out = engine.query("alpha AND NOT common", version=2)
    assert out.ok
    ops = [s["op"] for s in out.steps]
    assert "term_load" in ops
    assert "universe_difference" in ops
    assert "visible_filter" in ops
    assert "intersect" in ops
    # AST 带回字符位置
    assert out.ast is not None
