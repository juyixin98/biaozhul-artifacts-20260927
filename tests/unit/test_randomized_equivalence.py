"""属性测试：随机表达式 × 随机语料 × 三种执行顺序，全面对照独立 oracle。

每轮：
- 随机生成可见全集与若干 term 集合（既有稀疏也有稠密），提交成版本；
- 再做一次随机删除形成新版本（验证删除同步可见性）；
- 随机生成布尔表达式（含 NOT、*、括号），三种执行顺序的结果必须逐 ID 一致，
  且都等于独立 oracle 的集合代数答案。
"""
from __future__ import annotations

import random

import pytest

from app.query import planner as PL
from app.query.engine import QueryEngine

from tests._fixtures import expected

TERMS = ["alpha", "beta", "gamma", "delta", "epsilon", "rare", "common", "ghost"]


def _expr(rng: random.Random, depth: int) -> str:
    if depth <= 0 or rng.random() < 0.3:
        choice = rng.random()
        if choice < 0.1:
            return "*"
        if choice < 0.2:
            return f"NOT {_expr(rng, depth - 1)}"
        return rng.choice(TERMS + ["unknown_zzz"])
    op = rng.choice(["AND", "OR", "AND", "OR", "NOT"])
    if op == "NOT":
        return f"NOT {_expr(rng, depth - 1)}"
    left = _expr(rng, depth - 1)
    right = _expr(rng, depth - 1)
    if rng.random() < 0.4:
        return f"({left}) {op} {right}"
    return f"{left} {op} {right}"


@pytest.mark.parametrize("seed", range(40))
def test_random_expressions_three_orders_match_oracle(store, seed):
    rng = random.Random(1000 + seed)
    # 1) 随机语料
    n_docs = rng.randint(0, 40)
    doc_terms: dict[int, list[str]] = {}
    for doc_id in range(1, n_docs + 1):
        k = rng.randint(0, 6)
        doc_terms[doc_id] = rng.sample(TERMS, k) if k else []
    if doc_terms:
        v1 = store.commit(adds={d: " ".join(ts) for d, ts in doc_terms.items()})
        deletes = rng.sample(sorted(doc_terms), rng.randint(0, len(doc_terms)))
        # 保证 v2 是真实的新版本：删除集合为空时追加一篇占位文档
        adds_v2 = {}
        if not deletes:
            adds_v2 = {n_docs + 1: "epsilon"}
        v2 = store.commit(adds=adds_v2, deletes=deletes)
    else:
        # 空全集 v0/v1，再加一删得到 v2
        v1 = store.commit(adds={1: "alpha"})
        v2 = store.commit(deletes=[1])
    eng = QueryEngine(store)

    for version in [0, v1, v2]:
        for _ in range(6):
            expr = _expr(rng, rng.randint(1, 4))
            want = expected(store, expr, version)
            results = {}
            for order in PL.ALL_ORDERS:
                out = eng.query(expr, version=version, order=order)
                assert out.ok, (
                    f"seed={seed} v{version} {expr!r} [{order}] "
                    f"{out.error_category}: {out.error_message}"
                )
                results[order] = out.result
                # 唯一有序不变量
                assert out.result == sorted(set(out.result))
            assert results[PL.ORDER_TEXTUAL] == results[PL.ORDER_REVERSE], expr
            assert results[PL.ORDER_RARE_FIRST] == results[PL.ORDER_TEXTUAL], expr
            assert set(results[PL.ORDER_RARE_FIRST]) == want, (
                f"v{version} expr={expr!r} got={set(results['rare_first'])} want={want}"
            )
