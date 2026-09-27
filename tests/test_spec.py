"""文本规范（词法/语法）单元测试。

断言具体 AST 结构与具体失败类别，而不是“接口能调用”。
"""
from __future__ import annotations

import pytest

from app.query.spec import (
    And,
    Not,
    Or,
    QueryError,
    Term,
    collect_terms,
    parse_query,
)


def test_simple_and_is_binary_left_assoc_chain():
    node = parse_query("cat AND dog")
    assert isinstance(node, And)
    assert node.pos == 4
    assert [c.term for c in node.children] == ["cat", "dog"]


def test_or_and_not_precedence():
    node = parse_query("cat OR dog AND NOT fish")
    # OR 在最外层
    assert isinstance(node, Or)
    left, right = node.children
    assert isinstance(left, Term) and left.term == "cat"
    assert isinstance(right, And)
    # AND 的右孩子是 NOT fish
    assert isinstance(right.children[1], Not)
    assert right.children[1].child.term == "fish"


def test_parentheses_override_precedence():
    node = parse_query("(cat OR dog) AND NOT fish")
    assert isinstance(node, And)
    or_node, not_node = node.children
    assert isinstance(or_node, Or)
    assert isinstance(not_node, Not)
    assert not_node.child.term == "fish"


def test_double_not():
    node = parse_query("NOT NOT cat")
    assert isinstance(node, Not)
    assert isinstance(node.child, Not)
    assert node.child.child.term == "cat"


def test_keywords_case_insensitive_and_terms_case_preserved():
    node = parse_query("Cat aNd Dog")
    assert isinstance(node, And)
    assert [c.term for c in node.children] == ["Cat", "Dog"]


def test_quoted_terms():
    node = parse_query('"term with space" AND x')
    assert node.children[0].term == "term with space"


def test_collect_terms_dedup_keeps_order():
    node = parse_query("cat AND dog OR cat AND fish")
    assert collect_terms(node) == ["cat", "dog", "fish"]


@pytest.mark.parametrize(
    "text,marker",
    [
        ("", "空查询"),
        ("   ", "空查询"),
        ("cat AND", "期待词项"),
        ("(cat AND dog", "缺少右括号"),
        ("cat AND dog)", "多余 token"),
        ("cat dog", "多余 token"),  # 不支持隐式 AND
        ("AND cat", "意外的 token"),
        ('cat AND "', "未闭合的引号"),
        ("cat AND $", "非法字符"),
    ],
)
def test_parse_errors_are_specific(text, marker):
    with pytest.raises(QueryError) as exc:
        parse_query(text)
    assert marker in str(exc.value)
