"""Lexer unit tests: token classes, escaping, comments, placeholders.

These tests assert exact token kinds and spans — the point is that quote and
comment handling is implemented by a state machine, not by a global regex
that could mistake content inside a string for SQL.
"""

from __future__ import annotations

import pytest

from sqlguard.lexer import TokenKind, tokenize, LexError


def kinds(sql: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(sql).tokens]


def texts_of_kind(sql: str, kind: TokenKind) -> list[str]:
    return [t.text for t in tokenize(sql).tokens if t.kind is kind]


def test_string_doubled_quote_is_escape_not_terminator():
    # 'O''Brien :x' is ONE string token; :x must not become a placeholder
    res = tokenize("SELECT 'O''Brien :x'")
    strings = [t for t in res.tokens if t.kind is TokenKind.STRING]
    assert len(strings) == 1
    assert strings[0].value == "O'Brien :x"
    assert texts_of_kind("SELECT 'O''Brien :x'", TokenKind.PLACEHOLDER) == []


def test_string_with_all_placeholder_shapes_is_inert():
    sql = "SELECT 'a :b @c ? $1 ${t}'"
    res = tokenize(sql)
    assert texts_of_kind(sql, TokenKind.PLACEHOLDER) == []
    assert texts_of_kind(sql, TokenKind.SLOT) == []
    inert_texts = {o.text for o in res.inert_occurrences}
    assert {":b", "@c", "?", "$1", "${t}"} <= inert_texts


def test_line_comment_placeholders_are_inert():
    sql = "SELECT 1 -- :x ? $2\n"
    res = tokenize(sql)
    comments = [t for t in res.tokens if t.kind is TokenKind.COMMENT]
    assert len(comments) == 1
    assert texts_of_kind(sql, TokenKind.PLACEHOLDER) == []
    assert {o.text for o in res.inert_occurrences} >= {":x", "?", "$2"}


def test_block_comment_supports_nesting_and_is_inert():
    sql = "SELECT 1 /* outer /* nested */ still :x */"
    res = tokenize(sql)
    comments = [t for t in res.tokens if t.kind is TokenKind.COMMENT]
    assert len(comments) == 1
    assert texts_of_kind(sql, TokenKind.PLACEHOLDER) == []
    assert any(o.text == ":x" for o in res.inert_occurrences)


def test_unterminated_string_is_hard_error_with_span():
    with pytest.raises(LexError) as exc:
        tokenize("SELECT 'abc")
    assert "unterminated string" in exc.value.message
    assert exc.value.span is not None and exc.value.span.start == 7


def test_unterminated_block_comment_is_hard_error():
    with pytest.raises(LexError) as exc:
        tokenize("SELECT 1 /* oops")
    assert "unterminated block comment" in exc.value.message


def test_unterminated_slot_is_lex_error():
    with pytest.raises(LexError):
        tokenize("SELECT ${abc ")


def test_empty_slot_is_lex_error():
    with pytest.raises(LexError):
        tokenize("SELECT ${}")


def test_numbered_placeholder_zero_rejected():
    with pytest.raises(LexError):
        tokenize("SELECT $0")


def test_placeholder_styles_tokenize_distinctly():
    res = tokenize("SELECT ?, $2, :name, @other, ${tbl}")
    placeholders = [t for t in res.tokens if t.kind is TokenKind.PLACEHOLDER]
    slots = [t for t in res.tokens if t.kind is TokenKind.SLOT]
    assert [(p.style, p.value) for p in placeholders] == [
        ("?", "?"), ("$", "2"), (":", "name"), ("@", "other"),
    ]
    assert len(slots) == 1 and slots[0].value == "tbl"


def test_quoted_identifier_variants():
    sql = 'SELECT "a b", `c d`, [e f]'
    res = tokenize(sql)
    qi = [t.value for t in res.tokens if t.kind is TokenKind.QUOTED_IDENT]
    assert qi == ["a b", "c d", "e f"]


def test_keywords_and_idents_classified():
    res = tokenize("select orders from")
    kws = [t.value for t in res.tokens if t.kind is TokenKind.KEYWORD]
    assert kws == ["SELECT", "FROM"]
    idents = [t.text for t in res.tokens if t.kind is TokenKind.IDENT]
    assert idents == ["orders"]


def test_spans_are_correct_offsets():
    res = tokenize("SELECT id FROM x")
    ident = next(t for t in res.tokens if t.text == "id")
    assert (ident.span.start, ident.span.end) == (7, 9)


def test_unexpected_character_rejected():
    with pytest.raises(LexError):
        tokenize("SELECT #")


def test_multiline_line_and_column_tracking():
    res = tokenize("SELECT 1\nSELECT 2")
    idents = [t for t in res.tokens if t.kind is TokenKind.KEYWORD]
    assert idents[0].span.line == 1 and idents[1].span.line == 2
