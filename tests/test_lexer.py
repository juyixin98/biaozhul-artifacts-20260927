"""Lexer unit tests: assert exact token streams for tricky lexical cases.

These expectations are authored against SQLite lexical rules, *not* against
the parser/kernel — the test oracle is the language spec.
"""

from __future__ import annotations

import pytest

from sqlguard.core.lexer import LexError, TokenKind, tokenize


def sig_tokens(sql: str):
    """Strip trivia; return (kind, value) pairs for exact matching."""
    return [(t.kind, t.value) for t in tokenize(sql)
            if t.kind is not TokenKind.EOF]


def test_single_quote_doubling_is_unescaped_into_one_token():
    toks = tokenize("'it''s a ? inside :name'")
    strings = [t for t in toks if t.kind is TokenKind.STRING]
    assert len(strings) == 1
    assert strings[0].value == "it's a ? inside :name"
    # placeholders inside the string must NOT be tokenized
    assert not [t for t in toks if t.kind is TokenKind.BIND_PARAM]


def test_double_quote_doubling_for_identifiers():
    toks = tokenize('"we""ird""col"')
    ids = [t for t in toks if t.kind is TokenKind.IDENTIFIER]
    assert len(ids) == 1
    assert ids[0].value == 'we"ird"col'


def test_backtick_and_bracket_identifiers():
    assert sig_tokens("`col`") == [(TokenKind.IDENTIFIER, "col")]
    assert sig_tokens("[col]") == [(TokenKind.IDENTIFIER, "col")]


def test_placeholder_in_line_comment_is_inert():
    toks = tokenize("SELECT ? -- :notparam ?123 @x\nFROM t")
    markers = [t.value for t in toks if t.kind is TokenKind.BIND_PARAM]
    assert markers == ["?"]
    comments = [t for t in toks if t.kind is TokenKind.LINE_COMMENT]
    assert len(comments) == 1
    assert ":notparam" in comments[0].value


def test_placeholder_in_block_comment_is_inert():
    toks = tokenize("SELECT /* ? :x $y @z */ ?")
    markers = [t.value for t in toks if t.kind is TokenKind.BIND_PARAM]
    assert markers == ["?"]
    assert any(t.kind is TokenKind.BLOCK_COMMENT for t in toks)


def test_nested_block_comments_sqlite_style():
    toks = tokenize("/* outer /* inner */ still outer */ ?")
    markers = [t.value for t in toks if t.kind is TokenKind.BIND_PARAM]
    assert markers == ["?"]
    comments = [t for t in toks if t.kind is TokenKind.BLOCK_COMMENT]
    assert len(comments) == 1
    # the question mark was inside comment text, token after comment is param
    assert toks[-2].kind is TokenKind.BIND_PARAM


def test_all_parameter_marker_shapes():
    sql = "? ?7 :name @at $dollar"
    markers = [t.value for t in tokenize(sql) if t.kind is TokenKind.BIND_PARAM]
    assert markers == ["?", "?7", ":name", "@at", "$dollar"]


def test_slot_tokenization_with_spaces():
    toks = tokenize("ORDER BY {{ sort_col }} {{sort_dir}}")
    slots = [(t.kind, t.value) for t in toks if t.kind is TokenKind.SLOT]
    assert slots == [(TokenKind.SLOT, "sort_col"), (TokenKind.SLOT, "sort_dir")]


def test_slot_inside_string_is_data_not_a_slot():
    toks = tokenize("'{{ not a slot }}'")
    assert not [t for t in toks if t.kind is TokenKind.SLOT]
    assert [t for t in toks if t.kind is TokenKind.STRING]


@pytest.mark.parametrize("bad", [
    "'unterminated",
    '"unterminated',
    "`unterminated",
    "[unterminated",
    "{{ unterminated ",
    "{{ x } ",
    "'",
    "/* never closed",
])
def test_lex_errors_report_position(bad):
    with pytest.raises(LexError) as ei:
        tokenize(bad)
    assert ei.value.offset >= 0
    assert ei.value.line >= 0


def test_keywords_are_upper_cased_words_preserved():
    toks = sig_tokens("select Name from Users")
    assert toks[0] == (TokenKind.KEYWORD, "SELECT")
    assert toks[1] == (TokenKind.WORD, "Name")
    assert toks[2] == (TokenKind.KEYWORD, "FROM")
    assert toks[3] == (TokenKind.WORD, "Users")


def test_line_and_column_tracking_after_newlines():
    toks = tokenize("SELECT 1\nSELECT 2")
    second_select = [t for t in toks
                     if t.kind is TokenKind.KEYWORD and t.value == "SELECT"][1]
    assert second_select.line == 1
    assert second_select.col == 0


def test_string_with_escaped_quote_attack_looks_like_placeholder():
    # classic payload shape; lexically it is ONE string, no params
    sql = "x = 'admin'' OR ''1''=''1 -- ?'"
    toks = tokenize(sql)
    strings = [t.value for t in toks if t.kind is TokenKind.STRING]
    assert strings == ["admin' OR '1'='1 -- ?"]
    assert not [t for t in toks if t.kind is TokenKind.BIND_PARAM]


def test_blob_literal_and_numbers():
    kinds = sig_tokens("x'4142' 12 1.5e-3 0xff")
    assert kinds[0] == (TokenKind.BLOB, "4142")
    assert kinds[1][0] is TokenKind.NUMBER
    assert kinds[2] == (TokenKind.NUMBER, "1.5e-3")
    assert kinds[3] == (TokenKind.NUMBER, "0xff")
