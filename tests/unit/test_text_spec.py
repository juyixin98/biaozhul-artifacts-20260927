"""查询文本规范测试：断言优先级、括号、具体失败类别与字符位置。"""
from __future__ import annotations

import pytest

from app.text import spec
from app.text.spec import (
    CATEGORY_DANGLING_OPERATOR,
    CATEGORY_EMPTY,
    CATEGORY_UNEXPECTED_END,
    CATEGORY_UNEXPECTED_TOKEN,
    CATEGORY_UNMATCHED_CLOSE,
    CATEGORY_UNMATCHED_OPEN,
    SpecError,
)


def test_precedence_not_then_and_then_or():
    node = spec.parse_query("a OR b AND NOT c")
    assert isinstance(node, spec.Or)
    assert isinstance(node.children[0], spec.Term) and node.children[0].value == "a"
    rhs = node.children[1]
    assert isinstance(rhs, spec.And)
    assert isinstance(rhs.children[1], spec.Not)
    assert rhs.children[1].child.value == "c"


def test_parentheses_override_precedence():
    node = spec.parse_query("(a OR b) AND c")
    assert isinstance(node, spec.And)
    assert isinstance(node.children[0], spec.Or)


def test_keywords_case_insensitive_terms_lowercased():
    node = spec.parse_query("ALPHA and Beta oR NOT Gamma")
    assert spec.canonical(node) == "((alpha AND beta) OR NOT gamma)"


def test_star_is_universe_atom():
    node = spec.parse_query("* AND a")
    assert isinstance(node, spec.And)
    assert isinstance(node.children[0], spec.Universe)


def test_cjk_term_supported():
    node = spec.parse_query("索引 AND 查询")
    assert isinstance(node, spec.And)
    assert node.children[0].value == "索引"


def test_double_not_needs_parens_or_is_nested_not():
    node = spec.parse_query("NOT NOT a")
    assert isinstance(node, spec.Not)
    assert isinstance(node.child, spec.Not)
    assert node.child.child.value == "a"


def test_canonical_roundtrip_stable():
    text = "a AND (b OR c) AND NOT d"
    assert spec.canonical(spec.parse_query(text)) == "(a AND (b OR c) AND NOT d)"


# ---- 具体失败类别（不是笼统 400）----


def test_empty_expression_category():
    with pytest.raises(SpecError) as ei:
        spec.parse_query("   ")
    assert ei.value.category == CATEGORY_EMPTY
    assert ei.value.position == 0


def test_dangling_and_reports_category_and_position():
    with pytest.raises(SpecError) as ei:
        spec.parse_query("a AND")
    e = ei.value
    assert e.category == CATEGORY_DANGLING_OPERATOR
    assert e.position == 5  # AND 之后的位置
    assert "AND" in e.message


def test_not_without_operand_at_end():
    with pytest.raises(SpecError) as ei:
        spec.parse_query("a AND NOT")
    assert ei.value.category == CATEGORY_UNEXPECTED_END


def test_unmatched_open_parenthesis():
    with pytest.raises(SpecError) as ei:
        spec.parse_query("(a AND b")
    assert ei.value.category == CATEGORY_UNMATCHED_OPEN
    assert ei.value.position == 0


def test_unmatched_close_parenthesis():
    with pytest.raises(SpecError) as ei:
        spec.parse_query("a)")
    assert ei.value.category == CATEGORY_UNMATCHED_CLOSE
    assert ei.value.position == 1


def test_illegal_character_position():
    with pytest.raises(SpecError) as ei:
        spec.parse_query("a @ b")
    assert ei.value.category == CATEGORY_UNEXPECTED_TOKEN
    assert ei.value.position == 2


def test_two_terms_without_operator_is_unexpected_token():
    # "a b"：解析完 a 后，b 是多余词素（不允许隐式 AND）
    with pytest.raises(SpecError) as ei:
        spec.parse_query("a b")
    assert ei.value.category == CATEGORY_UNEXPECTED_TOKEN
    assert ei.value.position == 1


def test_span_recorded_on_terms_for_diagnostics():
    node = spec.parse_query("foo AND bar")
    and_node = node
    assert and_node.children[0].span == (0, 3)
    assert and_node.children[1].span == (7, 11)


def test_grammar_spec_lists_all_categories():
    g = spec.grammar_spec()
    assert set(spec.ALL_CATEGORIES) <= set(g["error_categories"])
    assert "无限" in g["universe_semantics"]
