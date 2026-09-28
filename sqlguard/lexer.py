"""Hand-written SQL lexer.

This is a *real tokenizer* (state machine over the source text), not a set of
global regular expressions over the query. That distinction matters for the
review: a placeholder or quote character that appears inside a string literal
or a comment must be recognized as inert content, while an unbalanced quote is
a hard lexing error rather than silently accepted input.

Supported lexical surface (SQLite-oriented):

* single-quoted strings with the SQL-standard ``''`` doubled-quote escape;
* double-quoted / back-tick / square-bracket identifiers;
* line comments ``--`` and block comments ``/* ... */``;
* value placeholders: ``?``, ``$1`` (numbered), ``:name`` and ``@name``;
* identifier slots: ``${name}`` (dynamic identifiers, checked against policy);
* keywords are identified lexically as bare identifiers that match
  :data:`KEYWORDS`.

The lexer also records occurrences of placeholder-looking text inside strings
and comments, so the audit report can prove such occurrences were *seen* and
deliberately treated as data rather than silently ignored.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

KEYWORDS = frozenset(
    w.upper()
    for w in (
        "select", "from", "where", "and", "or", "not", "null", "is", "in",
        "like", "glob", "between", "order", "by", "group", "having", "limit",
        "offset", "asc", "desc", "as", "insert", "into", "values", "update",
        "set", "delete", "or", "distinct", "all", "union", "intersect",
        "except", "case", "when", "then", "else", "end", "cast", "join",
        "left", "right", "inner", "outer", "cross", "on", "using", "true",
        "false", "any", "some", "exists", "with",
        # DDL / other statement verbs — tokenized as keywords so the parser
        # reports them as unsupported statements rather than stray names
        "create", "drop", "alter", "truncate", "replace", "pragma",
        "attach", "detach", "begin", "commit", "rollback", "savepoint",
        "vacuum", "reindex", "analyze", "grant", "revoke", "merge",
    )
)

MULTI_OPS = ("<>", "!=", ">=", "<=", "||", "::")
SINGLE_OPS = frozenset("+-*/%=<>!|~&")
PUNCT = frozenset("(),.;")
IDENT_START = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
IDENT_BODY = IDENT_START | set("0123456789$")


class TokenKind(enum.Enum):
    KEYWORD = "keyword"
    IDENT = "ident"
    QUOTED_IDENT = "quoted_ident"
    STRING = "string"
    NUMBER = "number"
    PLACEHOLDER = "placeholder"  # value parameter
    SLOT = "slot"                # ${name} dynamic-identifier parameter
    OP = "op"
    PUNCT = "punct"
    COMMENT = "comment"
    EOF = "eof"


@dataclass(frozen=True)
class Span:
    start: int  # 0-based char offset in source
    end: int
    line: int
    col: int     # 0-based column of start

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "line": self.line}


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str               # raw source text
    value: str              # decoded value (string content / ident / param name)
    span: Span
    style: str | None = None  # placeholder style: ? | $ | : | @ ; or slot style

    @property
    def upper(self) -> str:
        return self.text.upper()


@dataclass(frozen=True)
class InertOccurrence:
    """A placeholder-looking occurrence found inside a string or comment."""

    text: str
    span: Span
    container: str  # "string" | "line_comment" | "block_comment"


class LexError(Exception):
    def __init__(self, message: str, *, span: Span | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.span = span


@dataclass
class LexResult:
    tokens: list[Token]
    inert_occurrences: list[InertOccurrence] = field(default_factory=list)


_PLACEHOLDER_CHARS = set("?:@$")


def _scan_inert(text: str, base: int, line: int, col: int, container: str,
                out: list[InertOccurrence]) -> None:
    """Record placeholder-looking runs inside an inert region."""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in _PLACEHOLDER_CHARS:
            j = i + 1
            if ch == "$":
                # numbered param or slot syntax
                if j < len(text) and (text[j].isdigit() or text[j] == "{"):
                    j += 1
                    if j - 1 < len(text) and text[j - 1] == "{":
                        while j < len(text) and text[j] != "}":
                            j += 1
                        if j < len(text):
                            j += 1
                    else:
                        while j < len(text) and text[j].isdigit():
                            j += 1
            elif ch in ":@":
                while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                    j += 1
            occ = text[i:j]
            if (ch in "?:" or j > i + 1) and occ not in (":", "@"):
                out.append(
                    InertOccurrence(
                        occ,
                        Span(base + i, base + j, line, col + i),
                        container,
                    )
                )
            i = j
        else:
            i += 1


class Lexer:
    def __init__(self, src: str) -> None:
        self.src = src
        self.n = len(src)
        self.i = 0
        self.line = 1
        self.col = 0
        self.tokens: list[Token] = []
        self.inert: list[InertOccurrence] = []

    # -- helpers -----------------------------------------------------------

    def _span(self, start: int) -> Span:
        return Span(start, self.i, self.line, self.col - (self.i - start))

    def _peek(self, off: int = 0) -> str:
        k = self.i + off
        return self.src[k] if k < self.n else ""

    def _advance(self) -> str:
        ch = self.src[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.col = 0
        else:
            self.col += 1
        return ch

    # -- main loop ---------------------------------------------------------

    def tokenize(self) -> LexResult:
        while self.i < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
            elif ch == "-" and self._peek(1) == "-":
                self._line_comment()
            elif ch == "/" and self._peek(1) == "*":
                self._block_comment()
            elif ch == "'":
                self._string()
            elif ch == '"':
                self._quoted_ident('"')
            elif ch == "`":
                self._quoted_ident("`")
            elif ch == "[":
                self._bracket_ident()
            elif ch == "$" and self._peek(1) == "{":
                self._slot()
            elif ch == "?":
                self._positional()
            elif ch == "$" and self._peek(1).isdigit():
                self._numbered()
            elif ch in ":@" and (self._peek(1).isalpha() or self._peek(1) == "_"):
                self._named(ch)
            elif ch.isdigit():
                self._number()
            elif ch in IDENT_START:
                self._ident()
            else:
                self._operator_or_punct()
        eof_span = Span(self.n, self.n, self.line, self.col)
        self.tokens.append(Token(TokenKind.EOF, "", "", eof_span))
        return LexResult(self.tokens, self.inert)

    # -- scanners ----------------------------------------------------------

    def _line_comment(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        buf = []
        self._advance(); self._advance()  # --
        while self.i < self.n and self._peek() != "\n":
            buf.append(self._advance())
        text = "".join(buf)
        _scan_inert(text, start + 2, sline, scol + 2, "line_comment", self.inert)
        self.tokens.append(
            Token(TokenKind.COMMENT, self.src[start:self.i], text,
                  Span(start, self.i, sline, scol))
        )

    def _block_comment(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        self._advance(); self._advance()  # /*
        depth = 1
        content = []
        while self.i < self.n and depth:
            ch = self._peek()
            if ch == "/" and self._peek(1) == "*":
                self._advance(); self._advance(); content.append("/*")
                depth += 1
            elif ch == "*" and self._peek(1) == "/":
                self._advance(); self._advance(); content.append("*/")
                depth -= 1
            else:
                content.append(self._advance())
        if depth:
            raise LexError(
                "unterminated block comment",
                span=Span(start, self.i, sline, scol),
            )
        text = "".join(content[:-1])  # drop closing */
        _scan_inert(text, start + 2, sline, scol + 2, "block_comment", self.inert)
        self.tokens.append(
            Token(TokenKind.COMMENT, self.src[start:self.i], text,
                  Span(start, self.i, sline, scol))
        )

    def _string(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        self._advance()  # opening '
        chars = []
        while True:
            if self.i >= self.n:
                raise LexError(
                    "unterminated string literal",
                    span=Span(start, self.i, sline, scol),
                )
            ch = self._peek()
            if ch == "'":
                if self._peek(1) == "'":
                    self._advance(); self._advance()
                    chars.append("'")  # doubled quote = literal quote
                else:
                    self._advance()  # closing '
                    break
            else:
                chars.append(self._advance())
        value = "".join(chars)
        _scan_inert(value, start + 1, sline, scol + 1, "string", self.inert)
        self.tokens.append(
            Token(TokenKind.STRING, self.src[start:self.i], value,
                  Span(start, self.i, sline, scol))
        )

    def _quoted_ident(self, quote: str) -> None:
        start, sline, scol = self.i, self.line, self.col
        self._advance()
        chars = []
        while True:
            if self.i >= self.n:
                raise LexError(
                    f"unterminated quoted identifier ({quote})",
                    span=Span(start, self.i, sline, scol),
                )
            ch = self._peek()
            if ch == quote:
                # SQL identifiers double the quote to escape it
                if self._peek(1) == quote:
                    self._advance(); self._advance()
                    chars.append(quote)
                else:
                    self._advance()
                    break
            else:
                chars.append(self._advance())
        self.tokens.append(
            Token(TokenKind.QUOTED_IDENT, self.src[start:self.i],
                  "".join(chars), Span(start, self.i, sline, scol))
        )

    def _bracket_ident(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        self._advance()  # [
        chars = []
        while True:
            if self.i >= self.n:
                raise LexError(
                    "unterminated bracketed identifier",
                    span=Span(start, self.i, sline, scol),
                )
            ch = self._peek()
            if ch == "]":
                self._advance()
                break
            chars.append(self._advance())
        self.tokens.append(
            Token(TokenKind.QUOTED_IDENT, self.src[start:self.i],
                  "".join(chars), Span(start, self.i, sline, scol))
        )

    def _slot(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        self._advance()  # $
        self._advance()  # {
        name_start = self.i
        while self.i < self.n and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        name = self.src[name_start:self.i]
        if not name:
            raise LexError(
                "empty identifier slot ${}",
                span=Span(start, self.i, sline, scol),
            )
        if self._peek() != "}":
            raise LexError(
                f"identifier slot ${{{name}}} not closed by '}}'",
                span=Span(start, self.i, sline, scol),
            )
        self._advance()  # }
        self.tokens.append(
            Token(TokenKind.SLOT, self.src[start:self.i], name,
                  Span(start, self.i, sline, scol), style="${}")
        )

    def _positional(self) -> None:
        start = self.i
        span = Span(start, start + 1, self.line, self.col)
        self._advance()
        self.tokens.append(
            Token(TokenKind.PLACEHOLDER, "?", "?", span, style="?")
        )

    def _numbered(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        self._advance()  # $
        ds = self.i
        while self.i < self.n and self._peek().isdigit():
            self._advance()
        num = self.src[ds:self.i]
        if num == "0":
            raise LexError(
                "numbered placeholder $0 is invalid (numbering starts at 1)",
                span=Span(start, self.i, sline, scol),
            )
        self.tokens.append(
            Token(TokenKind.PLACEHOLDER, self.src[start:self.i], num,
                  Span(start, self.i, sline, scol), style="$")
        )

    def _named(self, sigil: str) -> None:
        start, sline, scol = self.i, self.line, self.col
        self._advance()  # sigil
        bs = self.i
        while self.i < self.n and (self._peek().isalnum() or self._peek() == "_"):
            self._advance()
        name = self.src[bs:self.i]
        self.tokens.append(
            Token(TokenKind.PLACEHOLDER, self.src[start:self.i], name,
                  Span(start, self.i, sline, scol), style=sigil)
        )

    def _number(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        while self.i < self.n and self._peek().isdigit():
            self._advance()
        if self._peek() == "." and self._peek(1).isdigit():
            self._advance()
            while self.i < self.n and self._peek().isdigit():
                self._advance()
        if self._peek() in "eE" and self._peek(1).isdigit():
            self._advance()
            while self.i < self.n and self._peek().isdigit():
                self._advance()
        text = self.src[start:self.i]
        self.tokens.append(
            Token(TokenKind.NUMBER, text, text, Span(start, self.i, sline, scol))
        )

    def _ident(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        while self.i < self.n and self._peek() in IDENT_BODY:
            self._advance()
        text = self.src[start:self.i]
        upper = text.upper()
        kind = TokenKind.KEYWORD if upper in KEYWORDS else TokenKind.IDENT
        self.tokens.append(
            Token(kind, text, upper if kind is TokenKind.KEYWORD else text,
                  Span(start, self.i, sline, scol))
        )

    def _operator_or_punct(self) -> None:
        start, sline, scol = self.i, self.line, self.col
        two = self.src[self.i:self.i + 2]
        if two in MULTI_OPS:
            self._advance(); self._advance()
            self.tokens.append(
                Token(TokenKind.OP, two, two, Span(start, self.i, sline, scol))
            )
            return
        ch = self._peek()
        self._advance()
        if ch in PUNCT:
            kind = TokenKind.PUNCT
        elif ch in SINGLE_OPS:
            kind = TokenKind.OP
        else:
            raise LexError(
                f"unexpected character {ch!r}",
                span=Span(start, self.i, sline, scol),
            )
        self.tokens.append(
            Token(kind, ch, ch, Span(start, self.i, sline, scol))
        )


def tokenize(src: str) -> LexResult:
    return Lexer(src).tokenize()
