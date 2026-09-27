"""Tokenizer for the search DSL.

Token kinds
-----------
  TERM     bare word (letters/digits/internal ``_-.@/``). Uppercase
           ``AND`` / ``OR`` / ``NOT`` are *words* too — the parser decides
           whether they act as operators based on position. That keeps a
           quoted or otherwise-typed ``AND`` from being special.
  FIELD    word immediately followed by ``:``; carries ``name`` and
           ``value`` (the word part). Whitespace before ``:`` makes it a
           plain TERM, so ``title :cat`` is two words, not a qualifier.
  PHRASE   double-quoted string; ``value`` is the *decoded* text and
           nothing inside it is ever an operator (``"a AND b"`` stays one
           token). Escapes: ``\\"``, ``\\\\``, ``\\n``, ``\\t``.
  LPAREN / RPAREN
  LBRACK / RBRACK   inclusive/exclusive range bounds ``[ ] { }``
  TO       the word ``TO`` (only special inside a range).
  COLON    a stray ``:`` not attached to a word (a syntax error later).

Backslash escape handling is explicit: a trailing backslash outside a
string raises UNTERMINATED_ESCAPE, and inside a string it either begins a
valid escape or falls back to the literal next character.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Optional

from searchdsl.astnodes import Pos
from searchdsl.errors import ErrorLocation, QuerySyntaxError

# Additional punctuation allowed *inside* a bare word (not at the start).
# '*' is included so open range bounds ([* TO x]) tokenize as a TERM; the
# parser only treats '*' specially inside a range.
_WORD_PUNCT = set("_-.@/")
_WORD_INTERNAL_EXTRA = set("*")
_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}


def _is_word_punct(ch: str) -> bool:
    return ch in _WORD_PUNCT


def _is_word_char(ch: str) -> bool:
    """Unicode letters/marks, decimal digits, connector punctuation."""
    if ch in _WORD_PUNCT or ch in _WORD_INTERNAL_EXTRA:
        return True
    cat = unicodedata.category(ch)
    # L* letters, M* marks, N* numbers, Pc connector punctuation.
    return cat[0] in {"L", "M", "N"} or cat == "Pc"


def _is_word_start(ch: str) -> bool:
    if ch in _WORD_PUNCT or ch == "*":
        return True
    cat = unicodedata.category(ch)
    return cat[0] in {"L", "M", "N"} or cat in {"Pc"}


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: Pos
    # For FIELD tokens: the field name and span of the name only.
    name: Optional[str] = None
    name_end: Optional[int] = None

    @property
    def end(self) -> int:
        return self.pos.end


class Lexer:
    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.i = 0

    # -- helpers ---------------------------------------------------------

    def _error(self, code: str, message: str, start: int, end: Optional[int] = None):
        raise QuerySyntaxError(
            message,
            pos=ErrorLocation(start, self.i if end is None else end),
            code=code,
        )

    def _skip_ws(self):
        while self.i < self.n and self.text[self.i].isspace():
            self.i += 1

    def _read_word(self) -> tuple[str, int]:
        start = self.i
        while self.i < self.n and _is_word_char(self.text[self.i]):
            self.i += 1
        return self.text[start:self.i], start

    def _read_phrase(self) -> tuple[str, int, int]:
        # self.i points at the opening quote.
        start = self.i
        self.i += 1
        chars: list[str] = []
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == '"':
                value = "".join(chars)
                self.i += 1
                return value, start, self.i
            if ch == "\\":
                if self.i + 1 >= self.n:
                    self._error(
                        "UNTERMINATED_ESCAPE",
                        "dangling backslash escape inside quoted string",
                        self.i,
                        self.n,
                    )
                nxt = self.text[self.i + 1]
                chars.append(_ESCAPES.get(nxt, nxt))
                self.i += 2
                continue
            chars.append(ch)
            self.i += 1
        raise QuerySyntaxError(
            "unterminated quoted string (missing closing '\"')",
            pos=ErrorLocation(start, self.n),
            code="UNTERMINATED_STRING",
        )

    # -- main loop -------------------------------------------------------

    def tokens(self) -> list[Token]:
        out: list[Token] = []
        while True:
            self._skip_ws()
            if self.i >= self.n:
                break
            ch = self.text[self.i]
            start = self.i

            if ch == '"':
                value, qs, qe = self._read_phrase()
                out.append(Token("PHRASE", value, Pos(qs, qe)))
                continue

            if ch == "\\":
                # Escapes are only meaningful in terms/phrases; a raw
                # backslash between words is an explicit error.
                self._error(
                    "UNTERMINATED_ESCAPE",
                    "unexpected backslash escape (use it inside a word or quotes)",
                    start,
                    start + 1,
                )

            if ch == "(":
                out.append(Token("LPAREN", ch, Pos(start, start + 1)))
                self.i += 1
                continue
            if ch == ")":
                out.append(Token("RPAREN", ch, Pos(start, start + 1)))
                self.i += 1
                continue
            if ch in "[{":
                out.append(Token("LBRACK", ch, Pos(start, start + 1)))
                self.i += 1
                continue
            if ch in "]}":
                out.append(Token("RBRACK", ch, Pos(start, start + 1)))
                self.i += 1
                continue
            if ch == ":":
                out.append(Token("COLON", ch, Pos(start, start + 1)))
                self.i += 1
                continue

            if _is_word_start(ch):
                word, wstart = self._read_word()
                # Field qualifier only when ':' directly follows the word.
                if self.i < self.n and self.text[self.i] == ":":
                    colon_at = self.i
                    self.i += 1  # consume ':'
                    out.append(
                        Token(
                            "FIELD",
                            word,
                            Pos(wstart, self.i),
                            name=word,
                            name_end=colon_at,
                        )
                    )
                    continue
                kind = "TO" if word == "TO" else "TERM"
                out.append(Token(kind, word, Pos(wstart, self.i)))
                continue

            self._error(
                "UNEXPECTED_TOKEN",
                f"unexpected character {ch!r}",
                start,
                start + 1,
            )

        return out


def lex(text: str) -> list[Token]:
    return Lexer(text).tokens()
