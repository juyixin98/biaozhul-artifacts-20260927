"""字段白名单/类型校验与复杂度预算断言（执行前失败，带类别与位置）。"""

import pytest

from searchdsl.budget import Budget, enforce, measure
from searchdsl.errors import DslError, ErrorCategory
from searchdsl.parser import parse

TEST_BUDGET = Budget(max_depth=8, max_clauses=64,
                     max_phrase_terms=16, max_term_length=128)


# -- 字段白名单与类型（对应 docs/ambiguity.md E5/E6） ------------------------
def test_unknown_field_rejected(engine) -> None:
    with pytest.raises(DslError) as exc:
        engine.run("foo:bar")
    err = exc.value
    assert err.category is ErrorCategory.FIELD_UNKNOWN
    assert err.position == 1
    assert "foo" in err.message


def test_int_field_non_integer_rejected(engine) -> None:
    with pytest.raises(DslError) as exc:
        engine.run("year:abc")
    err = exc.value
    assert err.category is ErrorCategory.FIELD_TYPE
    assert err.position == 1


def test_phrase_on_int_field_rejected(engine) -> None:
    with pytest.raises(DslError) as exc:
        engine.run('year:"2021 2022"')
    assert exc.value.category is ErrorCategory.FIELD_TYPE
    assert exc.value.position == 1


def test_unknown_field_inside_nested_tree(engine) -> None:
    with pytest.raises(DslError) as exc:
        engine.run("apple AND nope:x OR b")
    assert exc.value.category is ErrorCategory.FIELD_UNKNOWN
    assert exc.value.position == 11  # nope 起始列


def test_valid_int_field_accepted(engine) -> None:
    result = engine.run("year:2021")
    assert set(result.matches) == {"d1", "d7"}


def test_unknown_field_not_silently_dropped_by_normalization(engine) -> None:
    # 未知字段即使与恒真形式并列，也必须在校验阶段失败，不能被规范化丢弃
    with pytest.raises(DslError):
        engine.run("bogus:x AND bogus:x")


# -- 复杂度预算 ---------------------------------------------------------------
def test_depth_budget_nested_and() -> None:
    # 深度 9：a AND (b AND (c AND ...))，每包一层 AND 深 1
    text = "a"
    for letter in "bcdefghi":  # 8 个额外 AND 层 -> 深度 9
        text = f"{text} AND ({letter}"
    text += ")" * 8
    tree = parse(text)
    assert measure(tree).depth == 9
    with pytest.raises(DslError) as exc:
        enforce(tree, TEST_BUDGET)
    assert exc.value.category is ErrorCategory.BUDGET


def test_depth_budget_not_chain() -> None:
    tree = parse("NOT " * 9 + "a")
    assert measure(tree).depth == 10
    with pytest.raises(DslError):
        enforce(tree, TEST_BUDGET)


def test_clause_budget_clause_inflation() -> None:
    tree = parse(" ".join(f"w{i}" for i in range(65)))  # 65 个隐式 AND 子句
    assert measure(tree).clauses == 65
    with pytest.raises(DslError) as exc:
        enforce(tree, TEST_BUDGET)
    assert exc.value.category is ErrorCategory.BUDGET
    assert "65" in exc.value.message


def test_budget_checks_raw_tree_before_normalization() -> None:
    # 右嵌套 + 重复词：原始树深 9，规范化后拍平+去重为单个词（深 1）。
    # 预算必须在规范化之前对原始树失败。
    text = "apple"
    for _ in range(8):
        text = f"apple AND ({text})"
    tree = parse(text)
    assert measure(tree).depth == 9
    assert measure(tree).clauses == 9
    with pytest.raises(DslError):
        enforce(tree, TEST_BUDGET)


def test_empty_query_always_within_budget() -> None:
    usage = enforce(parse(""), TEST_BUDGET)
    assert usage.depth == 0 and usage.clauses == 0


def test_boundary_depth_eight_passes() -> None:
    # 深度恰好 8 通过
    text = "a"
    for letter in "bcdefgh":  # 7 层
        text = f"{text} AND ({letter}"
    text += ")" * 7
    tree = parse(text)
    assert measure(tree).depth == 8
    usage = enforce(tree, TEST_BUDGET)
    assert usage.depth == 8


def test_phrase_term_budget() -> None:
    big_phrase = '"' + " ".join(f"w{i}" for i in range(17)) + '"'
    with pytest.raises(DslError) as exc:
        enforce(parse(big_phrase), TEST_BUDGET)
    assert exc.value.category is ErrorCategory.BUDGET


def test_engine_returns_413_category_for_deep_query(engine) -> None:
    text = "a"
    for letter in "bcdefghi":
        text = f"{text} AND ({letter}"
    text += ")" * 8
    with pytest.raises(DslError) as exc:
        engine.run(text)
    assert exc.value.category is ErrorCategory.BUDGET
