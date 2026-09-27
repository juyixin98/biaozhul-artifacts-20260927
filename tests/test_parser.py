"""语法断言：优先级、隐式 AND、短语、字段限定的显式 AST 与错误位置。

期望 AST 全部手工给出（不调用被测代码生成），与 docs/ambiguity.md 一一对应。
"""

import pytest

from searchdsl import ast_nodes as ast
from searchdsl.errors import DslError, ErrorCategory
from searchdsl.parser import parse

# -- 手工构造期望树的便捷函数 -------------------------------------------------
def T(value, field=None): return ast.Term(value=value, field=field)
def P(terms, field=None): return ast.Phrase(terms=tuple(terms), field=field)
def NOT(c): return ast.Not(c)
def AND(*cs): return ast.And(tuple(cs))
def OR(*cs): return ast.Or(tuple(cs))


def d(node: ast.Node) -> dict:
    return ast.to_dict(node)


# -- 歧义样例 A：优先级与隐式连接词 -------------------------------------------
def test_a1_implicit_and_below_or() -> None:
    assert d(parse("a OR b c")) == d(OR(T("a"), AND(T("b"), T("c"))))


def test_a2_not_binds_tighter_than_or() -> None:
    assert d(parse("NOT a OR b")) == d(OR(NOT(T("a")), T("b")))


def test_a3_and_not_implicit_third_clause() -> None:
    assert d(parse("a AND NOT b c")) == d(AND(T("a"), NOT(T("b")), T("c")))


def test_a4_or_at_top_level() -> None:
    assert d(parse("a AND b OR c AND d")) == d(
        OR(AND(T("a"), T("b")), AND(T("c"), T("d"))))


def test_a5_parentheses_change_precedence() -> None:
    assert d(parse("a AND (b OR c)")) == d(AND(T("a"), OR(T("b"), T("c"))))


def test_left_associative_without_parens() -> None:
    assert d(parse("a AND b AND c")) == d(AND(T("a"), T("b"), T("c")))
    assert d(parse("a OR b OR c")) == d(OR(T("a"), T("b"), T("c")))


# -- 歧义样例 B：引号与转义 ---------------------------------------------------
def test_b1_operator_inside_phrase_is_literal() -> None:
    assert d(parse('title:"a OR b"')) == d(P(["a", "or", "b"], field="title"))


def test_b2_escaped_colon_is_one_term() -> None:
    assert d(parse(r"c\:windows")) == d(T(r"c:windows"))


def test_b3_lowercase_keywords_are_terms() -> None:
    assert d(parse("salad and pie")) == d(AND(T("salad"), T("and"), T("pie")))


def test_field_term_and_phrase() -> None:
    assert d(parse("year:2021")) == d(T("2021", field="year"))
    assert d(parse('author:"bob"')) == d(P(["bob"], field="author"))


def test_empty_query_is_empty_node() -> None:
    assert d(parse("")) == {"type": "empty"}
    assert d(parse("   ")) == {"type": "empty"}


# -- 歧义样例 C：错误位置 -----------------------------------------------------
@pytest.mark.parametrize("text,pos", [
    ("a AND OR b", 7),       # E1
    ("(a OR b", 1),          # E2
    ("a : b", 3),            # E3
    ("title:", 7),           # E4
    ("(a OR b))", 9),        # E10
    ("a AND (b OR )", 13),   # E11
    ("AND", 1),              # 行首运算符
    ("a AND", 6),            # 查询残缺：AND 后无操作数（EOF 在 col 6）
])
def test_parse_error_positions(text: str, pos: int) -> None:
    with pytest.raises(DslError) as exc:
        parse(text)
    assert exc.value.category is ErrorCategory.PARSE
    assert exc.value.position == pos, f"{text!r}: 期望位置 {pos}，实际 {exc.value.position}"


def test_field_whitespace_after_colon_error() -> None:
    with pytest.raises(DslError) as exc:
        parse("title: apple")
    assert exc.value.category is ErrorCategory.PARSE
    assert exc.value.position == 6  # 冒号列


def test_empty_parens_error() -> None:
    with pytest.raises(DslError) as exc:
        parse("()")
    assert exc.value.category is ErrorCategory.PARSE
    assert exc.value.position == 2
