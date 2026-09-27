"""词法断言：token 序列、关键字、引号、转义、错误位置。"""

import pytest

from searchdsl.errors import DslError, ErrorCategory
from searchdsl.lexer import TokenKind, lex


def kinds(text: str) -> list[TokenKind]:
    return [t.kind for t in lex(text)]


def test_basic_token_stream() -> None:
    toks = lex("apple AND (year:2021 OR tags:fruit)")
    assert [(t.kind, t.col) for t in toks] == [
        (TokenKind.TERM, 1), (TokenKind.AND, 7), (TokenKind.LPAREN, 11),
        (TokenKind.TERM, 12), (TokenKind.COLON, 16), (TokenKind.TERM, 17),
        (TokenKind.OR, 22), (TokenKind.TERM, 25), (TokenKind.COLON, 29),
        (TokenKind.TERM, 30), (TokenKind.RPAREN, 35), (TokenKind.EOF, 36),
    ]


def test_uppercase_keywords_only() -> None:
    toks = lex("and or not AND OR NOT")
    assert [t.kind for t in toks] == [
        TokenKind.TERM, TokenKind.TERM, TokenKind.TERM,
        TokenKind.AND, TokenKind.OR, TokenKind.NOT, TokenKind.EOF,
    ]
    assert lex("and")[0].value == "and"


def test_phrase_terms_and_position() -> None:
    toks = lex('title:"a OR b" pie')
    phrase_tok = toks[2]  # 0=title, 1=:, 2=PHRASE
    assert phrase_tok.kind is TokenKind.PHRASE
    assert phrase_tok.phrase == ("a", "or", "b")  # OR 在引号内只是词（小写归一）
    assert phrase_tok.col == 7 and phrase_tok.end == 15
    assert toks[3].kind is TokenKind.TERM and toks[3].value == "pie"


def test_escapes_in_bare_term_and_phrase() -> None:
    assert lex(r"c\:windows")[0].value == "c:windows"
    assert lex(r'a\"b')[0].value == 'a"b'
    assert lex(r'"say \"hi\""')[0].phrase == ("say", "hi")
    assert lex(r"a\\b")[0].value == r"a\b"


def test_empty_input_is_eof_only() -> None:
    assert kinds("   ") == [TokenKind.EOF]


@pytest.mark.parametrize("text,pos", [
    ('"unterminated', 1),
    ('"', 1),
    ('a\\', 2),
    ('""', 1),
])
def test_lexer_error_positions(text: str, pos: int) -> None:
    with pytest.raises(DslError) as exc:
        lex(text)
    assert exc.value.category is ErrorCategory.LEXER
    assert exc.value.position == pos
