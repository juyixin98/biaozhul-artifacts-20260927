"""SQL tokenizer for the *review* dialect.

This is a genuine state-machine lexer, not a regex split. It knows the lexical
rules that matter for security review of SQLite-compatible SQL:

* string literals (single quoted) with ``''`` escaping;
* quoted identifiers (double quoted / backtick / square bracket);
* line and block comments (block comments nest, as in SQLite);
* parameter placeholders: ``?`` / ``?NNN`` / ``:name`` / ``@name`` / ``$name``;
* dynamic identifier slots: ``{{ slot_name }}`` (templating layer, never SQL);
* numbers, bare words, operators and punctuation.

Placeholders are *not* recognised inside strings or comments, which is exactly
the property the reviewer relies on (a ``?`` inside a string literal is data,
not a bind parameter).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TokenKind(str, Enum):
    WORD = "word"                 # bare identifier or keyword (parser decides)
    KEYWORD = "keyword"           # reserved word, upper-cased in .value
    STRING = "string"             # 'it''s' -> raw value is it's
    IDENTIFIER = "identifier"     # "col" / `col` / [col] -> raw col name
    NUMBER = "number"
    BLOB = "blob"                 # x'...'
    BIND_PARAM = "bind_param"     # ? / ?12 / :name / @name / $name
    SLOT = "slot"                 # {{ name }}
    LINE_COMMENT = "line_comment"
    BLOCK_COMMENT = "block_comment"
    OP = "op"                     # multi-char operators
    PUNCT = "punct"               # single-char punctuation
    EOF = "eof"


# SQLite reserved words we care about structurally. A superset is fine.
KEYWORDS = frozenset(
    """
    ABORT ACTION ADD AFTER ALL ALTER ANALYZE AND AS ASC ATTACH AUTOINCREMENT
    BEFORE BEGIN BETWEEN BY CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT
    CONFLICT CONSTRAINT CREATE CROSS CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
    DATABASE DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DROP EACH
    ELSE END ESCAPE EXCEPT EXCLUSIVE EXISTS EXPLAIN FAIL FOR FOREIGN FROM FULL
    GLOB GROUP HAVING IF IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER
    INSERT INSTEAD INTERSECT INTO IS ISNULL JOIN KEY LEFT LIKE LIMIT MATCH
    NATURAL NO NOT NOTNULL NULL OF OFFSET ON OR ORDER OUTER PLAN PRAGMA
    PRIMARY QUERY RAISE RECURSIVE REFERENCES REGEXP REINDEX RELEASE RENAME
    REPLACE RESTRICT RIGHT ROLLBACK ROW SAVEPOINT SELECT SET TABLE TEMP
    TEMPORARY THEN TO TRANSACTION TRIGGER UNION UNIQUE UPDATE USING VACUUM
    VALUES VIEW VIRTUAL WHEN WHERE WITH WITHOUT
    """.split()
)

_MULTI_OPS = ("||", "<=", ">=", "<>", "!=", "<<", ">>", "==", "->", "->>", "||=")
_SINGLE_OPS = set("+-*/%<>=&|~!")
_PUNCT = set("(),.;")


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    value: str
    start: int
    end: int
    line: int
    col: int

    def is_keyword(self, *names: str) -> bool:
        return self.kind is TokenKind.KEYWORD and self.value in {n.upper() for n in names}


class LexError(Exception):
    """Raised on lexical malformed input. Coordinates are 0-based offsets."""

    def __init__(self, message: str, offset: int, line: int, col: int):
        super().__init__(f"{message} at line {line + 1}, col {col + 1}")
        self.offset = offset
        self.line = line
        self.col = col


def tokenize(sql: str) -> list[Token]:
    """Tokenize *sql* into a list ending with a single ``EOF`` token."""
    tokens: list[Token] = []
    n = len(sql)
    i = 0
    line = 0
    line_start = 0

    def pos(p: int) -> tuple[int, int]:
        return line, p - line_start

    while i < n:
        c = sql[i]

        # --- whitespace (track newlines for diagnostics) ---
        if c in " \t\r\n\f\v":
            if c == "\n":
                line += 1
                line_start = i + 1
            i += 1
            continue

        start = i
        sl, sc = pos(i)

        # --- line comment -- and comments inside keep placeholders inert ---
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = i + 2
            while j < n and sql[j] != "\n":
                j += 1
            tokens.append(Token(TokenKind.LINE_COMMENT, sql[i:j], start, j, sl, sc))
            i = j
            continue

        # --- C-style block comment; SQLite allows nesting ---
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if sql[j] == "/" and j + 1 < n and sql[j + 1] == "*":
                    depth += 1
                    j += 2
                elif sql[j] == "*" and j + 1 < n and sql[j + 1] == "/":
                    depth -= 1
                    j += 2
                else:
                    if sql[j] == "\n":
                        line += 1
                        line_start = j + 1
                    j += 1
            if depth > 0:
                raise LexError("unterminated block comment", start, sl, sc)
            tokens.append(Token(TokenKind.BLOCK_COMMENT, sql[start:j], start, j, sl, sc))
            i = j
            continue

        # --- string literal ' with '' doubling; x'...' blobs ---
        if c in ("x", "X") and i + 1 < n and sql[i + 1] == "'":
            j = i + 2
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                if sql[j] == "\n":
                    line += 1
                    line_start = j + 1
                j += 1
            if j >= n:
                raise LexError("unterminated blob literal", start, sl, sc)
            tokens.append(Token(TokenKind.BLOB, sql[i + 2:j], start, j + 1, sl, sc))
            i = j + 1
            continue

        if c == "'":
            j = i + 1
            buf: list[str] = []
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                if sql[j] == "\n":
                    line += 1
                    line_start = j + 1
                buf.append(sql[j])
                j += 1
            if j >= n:
                raise LexError("unterminated string literal", start, sl, sc)
            tokens.append(Token(TokenKind.STRING, "".join(buf), start, j + 1, sl, sc))
            i = j + 1
            continue

        # --- quoted identifiers ---
        if c == '"' or c == "`":
            close = c
            j = i + 1
            buf = []
            while j < n:
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close:
                        buf.append(close)
                        j += 2
                        continue
                    break
                if sql[j] == "\n":
                    line += 1
                    line_start = j + 1
                buf.append(sql[j])
                j += 1
            if j >= n:
                kind = "quoted identifier"
                raise LexError(f"unterminated {kind}", start, sl, sc)
            tokens.append(Token(TokenKind.IDENTIFIER, "".join(buf), start, j + 1, sl, sc))
            i = j + 1
            continue

        if c == "[":
            j = i + 1
            while j < n and sql[j] != "]":
                if sql[j] == "\n":
                    line += 1
                    line_start = j + 1
                j += 1
            if j >= n:
                raise LexError("unterminated bracketed identifier", start, sl, sc)
            tokens.append(Token(TokenKind.IDENTIFIER, sql[i + 1:j], start, j + 1, sl, sc))
            i = j + 1
            continue

        # --- dynamic identifier slot {{ name }} ---
        if c == "{" and i + 1 < n and sql[i + 1] == "{":
            j = i + 2
            depth = 1
            while j < n and depth > 0:
                if sql[j] == "{":
                    depth += 1
                elif sql[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                raise LexError("unterminated identifier slot {{ ... }}", start, sl, sc)
            # require the matching closing double brace
            if j + 1 >= n or sql[j + 1] != "}":
                raise LexError("single '}' inside {{ ... }} slot", j, *pos(j))
            raw = sql[i + 2:j]
            name = raw.strip()
            if not name:
                raise LexError("empty identifier slot {{ }}", start, sl, sc)
            tokens.append(Token(TokenKind.SLOT, name, start, j + 2, sl, sc))
            i = j + 2
            continue

        if c == "}":
            # stray } or }} with no opener is malformed templating
            raise LexError("unexpected '}' with no matching '{{'", start, sl, sc)

        # --- bind parameters: ? ?NNN :name @name $name ---
        if c == "?":
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            tokens.append(Token(TokenKind.BIND_PARAM, sql[i:j], start, j, sl, sc))
            i = j
            continue
        if c in ":@$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "_$:"):
                j += 1
            if j == i + 1:
                raise LexError(f"parameter marker '{c}' is not followed by a name", start, sl, sc)
            tokens.append(Token(TokenKind.BIND_PARAM, sql[i:j], start, j, sl, sc))
            i = j
            continue

        # --- numbers (digits, decimal, exponent, hex 0x..) ---
        if c.isdigit() or (
            c == "." and i + 1 < n and sql[i + 1].isdigit()
        ):
            j = i
            if c == "0" and i + 1 < n and sql[i + 1] in "xX":
                j = i + 2
                while j < n and (sql[j] in "0123456789abcdefABCDEF"):
                    j += 1
            else:
                seen_digit = False
                while j < n and (sql[j].isdigit() or sql[j] == "."):
                    seen_digit |= sql[j].isdigit()
                    j += 1
                if j < n and sql[j] in "eE":
                    k = j + 1
                    if k < n and sql[k] in "+-":
                        k += 1
                    if k < n and sql[k].isdigit():
                        while k < n and sql[k].isdigit():
                            k += 1
                        j = k
                if not seen_digit:
                    raise LexError("malformed numeric literal", start, sl, sc)
            tokens.append(Token(TokenKind.NUMBER, sql[i:j], start, j, sl, sc))
            i = j
            continue

        # --- words / keywords ---
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_" or sql[j] == "$"):
                j += 1
            word = sql[i:j]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token(TokenKind.KEYWORD, upper, start, j, sl, sc))
            else:
                tokens.append(Token(TokenKind.WORD, word, start, j, sl, sc))
            i = j
            continue

        # --- operators (longest match) ---
        two = sql[i:i + 2]
        three = sql[i:i + 3]
        if three in _MULTI_OPS:
            tokens.append(Token(TokenKind.OP, three, start, i + 3, sl, sc))
            i += 3
            continue
        if two in _MULTI_OPS:
            tokens.append(Token(TokenKind.OP, two, start, i + 2, sl, sc))
            i += 2
            continue
        if c in _SINGLE_OPS:
            tokens.append(Token(TokenKind.OP, c, start, i + 1, sl, sc))
            i += 1
            continue
        if c in _PUNCT:
            tokens.append(Token(TokenKind.PUNCT, c, start, i + 1, sl, sc))
            i += 1
            continue

        raise LexError(f"unexpected character {c!r}", start, sl, sc)

    tokens.append(Token(TokenKind.EOF, "", n, n, line, n - line_start))
    return tokens
