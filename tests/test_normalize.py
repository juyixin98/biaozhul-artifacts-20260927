"""规范化断言：拍平、去重、排序、双重否定、幂等、空查询语义。"""

import pytest

from searchdsl import ast_nodes as ast
from searchdsl.normalize import is_idempotent, normalize
from searchdsl.parser import parse


def T(value, field=None): return ast.Term(value=value, field=field)
def NOT(c): return ast.Not(c)
def AND(*cs): return ast.And(tuple(cs))
def OR(*cs): return ast.Or(tuple(cs))


def tree(text: str) -> ast.Node:
    return parse(text)


def test_flatten_nested_and_or() -> None:
    # ((a AND b) AND c) 拍平为三元 AND；规范化子节点按序列化键排序
    got = normalize(tree("(a AND b) AND c"))
    assert got == AND(T("a"), T("b"), T("c"))
    assert ast.to_dict(got)["type"] == "and"
    assert [ast.to_dict(x)["value"] for x in got.children] == ["a", "b", "c"]


def test_dedupe_clauses() -> None:
    assert normalize(tree("apple AND apple")) == T("apple")
    assert normalize(tree("apple OR apple")) == T("apple")
    assert normalize(tree("a OR b OR a")) == OR(T("a"), T("b"))


def test_double_negation_elimination() -> None:
    assert normalize(tree("NOT NOT apple")) == T("apple")


def test_single_child_boolean_unwrapped() -> None:
    assert normalize(tree("(apple)")) == T("apple")
    assert normalize(AND(T("apple"))) == T("apple")
    assert normalize(OR(T("apple"))) == T("apple")


def test_sorted_canonical_order_is_stable() -> None:
    # 子节点顺序与书写无关：b AND a 与 a AND b 得到同一棵规范树
    assert ast.to_dict(normalize(tree("b AND a"))) == ast.to_dict(normalize(tree("a AND b")))
    assert ast.to_dict(normalize(tree("z OR a OR m"))) == ast.to_dict(
        normalize(tree("a OR m OR z")))


def test_not_child_sorts_by_canonical_key() -> None:
    # json 键序：NOT 节点键含 "child"，Term 节点键含 "value"，child < value
    got = normalize(tree("NOT a OR b"))
    assert ast.to_dict(got) == {
        "type": "or",
        "children": [
            {"type": "not", "child": {"type": "term", "field": None, "value": "a"}},
            {"type": "term", "field": None, "value": "b"},
        ],
    }


def test_empty_query_preserved() -> None:
    empty = normalize(tree(""))
    assert isinstance(empty, ast.Empty)
    assert ast.to_dict(empty) == {"type": "empty"}


@pytest.mark.parametrize("text", [
    "", "a", "NOT a", "a AND b", "a OR b", "a AND NOT b c",
    "(a OR b) AND (c OR d)", "NOT NOT NOT (a AND a OR b OR b)",
    'title:"a OR b" AND author:bob AND title:"a OR b"',
])
def test_normalize_is_idempotent(text: str) -> None:
    once = normalize(tree(text))
    twice = normalize(once)
    assert ast.to_dict(twice) == ast.to_dict(once)
    assert is_idempotent(tree(text)) is True


def test_equivalent_spellings_share_canonical_form() -> None:
    forms = [
        "a AND b AND c",
        "c AND b AND a",
        "(a AND b) c",
        "a b c",
    ]
    canon = {str(ast.to_dict(normalize(tree(f)))) for f in forms}
    assert len(canon) == 1
