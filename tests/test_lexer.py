"""Lexer tests: token kinds, exact positions, phrases and escapes.

Positions are asserted as [start, end) character offsets so error
location behavior is pinned down, not just "it parses".
"""

from __future__ import annotations

import pytest

from searchdsl.errors import QuerySyntaxError
from searchdsl.lexer import lex


def ts(text):
    return [(t.kind, t.value, [t.pos.start, t.pos.end]) for t in lex(text)]


def test_bare_words_and_implicit_spacing():
    assert ts("quick fox") == [
        ("TERM", "quick", [0, 5]),
        ("TERM", "fox", [6, 9]),
    ]


def test_field_qualifier_requires_adjacent_colon():
    assert ts("title:fox")[0] == ("FIELD", "title", [0, 6])
    toks = ts("title :fox")
    assert toks[0] == ("TERM", "title", [0, 5])
    assert toks[1] == ("COLON", ":", [6, 7])


def tk(text):
    return [(t.kind, t.value) for t in lex(text)]


def test_parens_brackets_and_to():
    assert tk("(a OR b)") == [
        ("LPAREN", "("), ("TERM", "a"), ("TERM", "OR"), ("TERM", "b"),
        ("RPAREN", ")"),
    ]
    assert tk("year:{1 TO 3}") == [
        ("FIELD", "year"), ("LBRACK", "{"), ("TERM", "1"),
        ("TO", "TO"), ("TERM", "3"), ("RBRACK", "}"),
    ]


def test_operators_inside_quotes_are_not_operators():
    toks = ts('"a AND b OR c (d)"')
    assert len(toks) == 1
    assert toks[0][0] == "PHRASE"
    assert toks[0][1] == "a AND b OR c (d)"
    # The phrase span includes both quotes.
    assert toks[0][2] == [0, 18]


def test_phrase_escapes():
    toks = lex(r'"say \"hi\"\nend"')
    assert len(toks) == 1
    assert toks[0].value == 'say "hi"\nend'
    assert toks[0].pos.start == 0 and toks[0].pos.end == 17


def test_unicode_terms():
    toks = ts("body:价格")
    assert toks[0] == ("FIELD", "body", [0, 5])
    assert toks[1] == ("TERM", "价格", [5, 7])


def test_unterminated_string_has_error_position():
    with pytest.raises(QuerySyntaxError) as ei:
        lex('"abc')
    assert ei.value.code == "UNTERMINATED_STRING"
    assert ei.value.pos.start == 0 and ei.value.pos.end == 4


def test_dangling_backslash_inside_string():
    with pytest.raises(QuerySyntaxError) as ei:
        lex('"abc' + "\\")
    assert ei.value.code == "UNTERMINATED_ESCAPE"
    # Position anchors on the backslash.
    assert ei.value.pos.start == 4


def test_bare_backslash_is_rejected():
    with pytest.raises(QuerySyntaxError) as ei:
        lex("a " + "\\" + " b")
    assert ei.value.code == "UNTERMINATED_ESCAPE"


def test_unexpected_character():
    with pytest.raises(QuerySyntaxError) as ei:
        lex("a$b")
    assert ei.value.code == "UNEXPECTED_TOKEN"
    assert ei.value.pos.start == 1
