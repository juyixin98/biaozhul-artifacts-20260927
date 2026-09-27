"""真值对照测试。

三套答案相互独立：
1. 手工算出的固定期望集合（本文件硬编码，不来自被测代码）；
2. ReferenceEvaluator：纯 Python 内存求值，与倒排索引实现完全独立
   （分词器也是另写的手写扫描版）；
3. Index（SQLite 倒排表）执行规范化后的树。

另外用固定种子随机生成 240 个小表达式，对每个表达式断言：
raw 参考真值 == normalized 参考真值 == 索引执行真值，且规范化幂等。
随机生成的每个表达式及其判定写入 logs/runs/<tag>/truth-cases.jsonl，可复核。
"""

import json
import random

import pytest

from searchdsl import ast_nodes as ast
from searchdsl.normalize import is_idempotent, normalize
from searchdsl.parser import parse

ALL = {f"d{i}" for i in range(1, 11)}


# -- 1. 手工期望（对照 fixtures/documents.json 逐篇数过） ---------------------
HAND_CASES = [
    # (查询, 期望命中文档集合, 说明)
    ("apple", {"d1", "d2", "d5", "d6", "d7", "d8", "d9"}, "默认字段 title+body"),
    ("apple AND pie", {"d1", "d8"}, "显式 AND"),
    ("apple pie", {"d1", "d8"}, "隐式 AND 等价显式 AND"),
    ("apple OR banana", {"d1", "d2", "d3", "d5", "d6", "d7", "d8", "d9"}, "OR"),
    ("apple AND NOT pie", {"d2", "d5", "d6", "d7", "d9"}, "NOT 取文档全集补集"),
    ("NOT apple", {"d3", "d4", "d10"}, "全补集"),
    ("walnut AND salad", {"d2"}, "交集"),
    ("apple AND (pie OR salad)", {"d1", "d2", "d8", "d9"}, "括号优先级"),
    ('"apple pie"', {"d1", "d8"}, "短语位置连续"),
    ("title:pie", {"d1", "d8"}, "字段限定 title"),
    ("author:alice", {"d1", "d3", "d8"}, "字段限定 author"),
    ("year:2021", {"d1", "d7"}, "int 字段等值"),
    ("tags:salad", {"d2", "d7", "d9"}, "tags 字段"),
    ("author:alice AND tags:dessert", {"d1", "d3"}, "跨字段 AND"),
    ("cinnamon AND rolls", {"d10"}, "两词同篇"),
    ("cherry OR cinnamon", {"d1", "d4", "d9", "d10"}, "OR 并集"),
    ('body:"fresh apple"', {"d1"}, "body 短语，d7 是 apple cider 不连续"),
    ("year:2020 AND tags:baking", {"d10"}, "int + text"),
    ("NOT NOT apple", {"d1", "d2", "d5", "d6", "d7", "d8", "d9"}, "双重否定"),
    ("apple AND apple", {"d1", "d2", "d5", "d6", "d7", "d8", "d9"}, "去重不改真值"),
    ("(apple OR banana) AND year:2023", {"d9"}, "apple/banana 且 2023：仅 d9"),
    ("greens AND NOT walnut", {"d7"}, "d2 greens+walnut 被排除"),
    ("PIE", {"d1", "d8"}, "大小写不敏感"),
    ("", ALL, "空查询匹配全部"),
    ("   ", ALL, "纯空白等同空查询"),
]


@pytest.mark.parametrize("query,expected,note", HAND_CASES)
def test_hand_computed_truth(engine, reference, query, expected, note) -> None:
    tree = parse(query)
    canonical = normalize(tree)
    index_hits = engine.index.execute(canonical)
    ref_raw = reference.evaluate(tree)
    ref_norm = reference.evaluate(canonical)
    assert ref_raw == expected, f"[{note}] 参考求值(原始) {sorted(ref_raw)} != 手工 {sorted(expected)}"
    assert index_hits == expected, f"[{note}] 索引执行 {sorted(index_hits)} != 手工 {sorted(expected)}"
    assert ref_norm == expected, f"[{note}] 参考求值(规范) {sorted(ref_norm)} != 手工 {sorted(expected)}"


# -- 2. 引擎端到端（含版本号、计数） ------------------------------------------
def test_engine_end_to_end_fields(engine) -> None:
    result = engine.run("apple AND (pie OR salad)")
    assert set(result.matches) == {"d1", "d2", "d8", "d9"}
    assert result.count == 4
    assert len(result.version) == 16
    assert result.idempotent is True
    stored = engine.store.get_query_version(result.version)
    assert stored is not None
    assert json_canonical(stored["canonical_json"]) == result.canonical


def json_canonical(blob: str) -> dict:
    return json.loads(blob)


def test_equivalent_spellings_share_version(engine) -> None:
    r1 = engine.run("apple AND pie")
    r2 = engine.run("pie AND apple")          # 排序后同一规范树
    r3 = engine.run("apple pie")              # 隐式 AND，同一规范树
    assert r1.version == r2.version == r3.version


# -- 3. 随机表达式：三套真值 + 幂等 -------------------------------------------
VOCAB = ["apple", "banana", "cherry", "walnut", "pie", "salad",
         "fresh", "cinnamon", "greens", "baking"]
INT_VALUES = ["2019", "2020", "2021", "2022", "2023", "2024"]
FIELD_WORDS = {"author": ["alice", "bob", "carol", "dave"],
               "tags": ["dessert", "fruit", "salad", "baking", "bread", "snack"],
               "title": ["pie", "apple", "salad", "bread"],
               "body": ["fresh", "with", "and"]}


def gen_operand(rng: random.Random) -> str:
    choice = rng.random()
    if choice < 0.65:
        return rng.choice(VOCAB)
    if choice < 0.8:
        field = rng.choice(list(FIELD_WORDS))
        return f"{field}:{rng.choice(FIELD_WORDS[field])}"
    if choice < 0.92:
        return f"year:{rng.choice(INT_VALUES)}"
    n = rng.randint(1, 2)
    return '"' + " ".join(rng.sample(VOCAB, n)) + '"'


def gen_expr(rng: random.Random, depth: int, budget: list[int]) -> str:
    if depth <= 0 or rng.random() < 0.45:
        operand = gen_operand(rng)
        budget[0] += 1
        if rng.random() < 0.12:
            operand = "NOT " + operand
        return operand
    op = rng.choice(["AND", "OR", "AND_IMPLICIT"])
    n = rng.randint(2, 3)
    parts = [gen_expr(rng, depth - 1, budget) for _ in range(n)]
    joiner = " " if op == "AND_IMPLICIT" else f" {op} "
    expr = joiner.join(parts)
    if rng.random() < 0.3:
        expr = f"({expr})"
    return expr


def test_random_expressions_truth_and_idempotence(engine, reference, run_dir) -> None:
    rng = random.Random(20260928)
    n_cases = 240
    case_log = run_dir / "truth-cases.jsonl"
    failures: list[str] = []
    with open(case_log, "w", encoding="utf-8") as log:
        for idx in range(n_cases):
            clause_budget = [0]
            expr = gen_expr(rng, depth=2, budget=clause_budget)
            # 复杂度预算保证在 8/64 内（depth=2、每节点最多 3 个子节点）
            try:
                raw = parse(expr)
                engine.settings.schema.validate(raw)
                canonical = normalize(raw)
                ref_raw = reference.evaluate(raw)
                ref_norm = reference.evaluate(canonical)
                idx_hits = engine.index.execute(canonical)
                ok = (ref_raw == ref_norm == idx_hits and is_idempotent(raw))
                verdict = "pass" if ok else "FAIL"
                log.write(json.dumps({
                    "case": idx, "expr": expr, "clauses": clause_budget[0],
                    "ref_raw": sorted(ref_raw), "ref_norm": sorted(ref_norm),
                    "index": sorted(idx_hits), "idempotent": is_idempotent(raw),
                    "verdict": verdict,
                }, ensure_ascii=False) + "\n")
                if not ok:
                    failures.append(f"case {idx}: {expr!r} ref={sorted(ref_raw)} "
                                    f"norm={sorted(ref_norm)} index={sorted(idx_hits)}")
            except Exception as exc:  # 生成的表达式理论上全部合法
                failures.append(f"case {idx}: {expr!r} raised {type(exc).__name__}: {exc}")
                log.write(json.dumps({"case": idx, "expr": expr,
                                      "verdict": "ERROR", "error": str(exc)}) + "\n")
    assert not failures, "随机真值对照失败:\n" + "\n".join(failures[:10])
    assert n_cases == 240


def test_random_case_count_respects_budget(engine) -> None:
    # 随机表达式：布尔深 2（最多 3*3=9 个叶子），叶子可能再被 NOT 包一层 -> 树深 ≤ 5
    rng = random.Random(20260928)
    for _ in range(240):
        clause_budget = [0]
        expr = gen_expr(rng, depth=2, budget=clause_budget)
        raw = parse(expr)
        assert clause_budget[0] <= 9
        assert max_depth_of(raw) <= 5


def max_depth_of(node: ast.Node) -> int:
    if isinstance(node, (ast.Term, ast.Phrase)):
        return 1
    if isinstance(node, ast.Not):
        return 1 + max_depth_of(node.child)
    if isinstance(node, (ast.And, ast.Or)):
        return 1 + max(max_depth_of(c) for c in node.children)
    return 0
