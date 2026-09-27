"""Parser tests: precedence, implicit conjunction, ambiguous examples.

The ambiguous cases each pin the *exact* AST (canonical JSON after
structure-preserving parsing) so there is one documented interpretation.
The raw parse tree is checked (not the normalized one) to show structure.
"""

from __future__ import annotations

import pytest

from searchdsl.astnodes import canonical_json, canonical_hash
from searchdsl.errors import QuerySyntaxError
from searchdsl.normalize import normalize
from searchdsl.parser import parse


def raw(query):
    return canonical_json(parse(query))


def norm(query):
    return canonical_json(normalize(parse(query)))


# --------------------------------------------------------------------------
# Precedence and implicit conjunction
# --------------------------------------------------------------------------

def test_implicit_and_between_words():
    assert raw("a b c") == (
        '{"children":['
        '{"field":null,"op":"term","value":"a"},'
        '{"field":null,"op":"term","value":"b"},'
        '{"field":null,"op":"term","value":"c"}'
        '],"op":"and"}'
    )


def test_and_binds_tighter_than_or():
    # a AND b OR c  ==  (a AND b) OR c
    tree = raw("a AND b OR c")
    assert tree == (
        '{"children":['
        '{"children":['
        '{"field":null,"op":"term","value":"a"},'
        '{"field":null,"op":"term","value":"b"}],"op":"and"},'
        '{"field":null,"op":"term","value":"c"}'
        '],"op":"or"}'
    )


def test_mixed_implicit_and_explicit_and():
    # a b OR c d == (a AND b) OR (c AND d)
    assert norm("a b OR c d") == (
        '{"children":['
        '{"children":['
        '{"field":null,"op":"term","value":"a"},'
        '{"field":null,"op":"term","value":"b"}],"op":"and"},'
        '{"children":['
        '{"field":null,"op":"term","value":"c"},'
        '{"field":null,"op":"term","value":"d"}],"op":"and"}'
        '],"op":"or"}'
    )


def test_not_binds_tighter_than_and():
    # NOT a b == (NOT a) AND b
    assert raw("NOT a b") == (
        '{"children":['
        '{"child":{"field":null,"op":"term","value":"a"},"op":"not"},'
        '{"field":null,"op":"term","value":"b"}'
        '],"op":"and"}'
    )


def test_double_not():
    assert norm("NOT NOT a") == '{"field":null,"op":"term","value":"a"}'


def test_parentheses_override_precedence():
    # a OR (b AND c) keeps OR at the top.
    tree = raw("a OR (b AND c)")
    assert tree.startswith('{"children":[{"field":null,"op":"term","value":"a"},'
                           '{"children":')
    assert '"op":"and"' in tree


def test_lowercase_and_is_a_word_not_operator():
    # and/or/not lowercase are ordinary search terms.
    toks_tree = raw("and or not")
    assert toks_tree.count('"op":"and"') == 1  # one implicit AND node
    assert '"value":"and"' in toks_tree
    assert '"value":"or"' in toks_tree
    assert '"value":"not"' in toks_tree


def test_quoted_and_is_a_phrase():
    assert raw('"a AND b"') == '{"field":null,"op":"phrase","value":"a AND b"}'


# --------------------------------------------------------------------------
# Fields, ranges, phrases
# --------------------------------------------------------------------------

def test_field_qualified_term_and_phrase():
    assert raw("title:cat") == '{"field":"title","op":"term","value":"cat"}'
    assert raw('body:"red fox"') == '{"field":"body","op":"phrase","value":"red fox"}'


def test_inclusive_and_exclusive_ranges():
    assert raw("year:[2000 TO 2010]") == (
        '{"field":"year","gt":null,"gte":"2000","lt":null,"lte":"2010","op":"range"}'
    )
    assert raw("year:{2000 TO 2010}") == (
        '{"field":"year","gt":"2000","gte":null,"lt":"2010","lte":null,"op":"range"}'
    )
    assert raw("year:[* TO 1999]") == (
        '{"field":"year","gt":null,"gte":null,"lt":null,"lte":"1999","op":"range"}'
    )


def test_quoted_colon_is_literal():
    # The ':' inside quotes never starts a field qualifier.
    assert raw('"a:b c"') == '{"field":null,"op":"phrase","value":"a:b c"}'


def test_empty_parens_is_match_all():
    assert norm("()") == '{"op":"match_all"}'


# --------------------------------------------------------------------------
# Error positions
# --------------------------------------------------------------------------

def test_dangling_and_error_position():
    with pytest.raises(QuerySyntaxError) as ei:
        parse("a AND")
    assert ei.value.code == "UNEXPECTED_TOKEN"
    assert [ei.value.pos.start, ei.value.pos.end] == [2, 5]


def test_leading_or_error_position():
    with pytest.raises(QuerySyntaxError) as ei:
        parse("OR a")
    assert ei.value.code == "UNEXPECTED_TOKEN"
    assert ei.value.pos.start == 0


def test_unmatched_open_paren_points_at_it():
    with pytest.raises(QuerySyntaxError) as ei:
        parse("(a b")
    assert ei.value.code == "UNBALANCED_PAREN"
    assert ei.value.pos.start == 0


def test_unmatched_close_paren_points_at_it():
    with pytest.raises(QuerySyntaxError) as ei:
        parse("a )")
    assert ei.value.code == "UNBALANCED_PAREN"
    assert ei.value.pos.start == 2


def test_missing_to_in_range():
    with pytest.raises(QuerySyntaxError) as ei:
        parse("year:[2000 2010]")
    assert ei.value.code == "RANGE_MALFORMED"


def test_field_without_value_points_at_field():
    with pytest.raises(QuerySyntaxError) as ei:
        parse("title:")
    assert ei.value.code == "UNEXPECTED_TOKEN"
    assert ei.value.pos.start == 0 and ei.value.pos.end == 6


def test_unfielded_range_is_rejected_later_by_validation_not_parser():
    tree = parse("[1 TO 5]")
    assert tree.field_name == ""


# --------------------------------------------------------------------------
# Canonical stability: identical queries hash identically
# --------------------------------------------------------------------------

def test_canonical_hash_independent_of_order_and_spacing():
    h1 = canonical_hash(normalize(parse("a b AND c")))
    h2 = canonical_hash(normalize(parse("c AND b a")))
    assert h1 == h2
