"""Recursive-descent parser.

Grammar (lowest to highest precedence)::

    query    := or_expr
    or_expr  := and_expr (OR and_expr)*
    and_expr := not_expr ((AND | <implicit>) not_expr)*
    not_expr := NOT not_expr | atom
    atom     := '(' or_expr ')'
              | range
              | field_prefix (TERM | PHRASE | range)
              | TERM | PHRASE

Precedence is therefore  NOT > AND (explicit and implicit) > OR.
``AND`` / ``OR`` / ``NOT`` are operators only when the uppercase token
occurs in a legal operator position; ``a AND b OR c`` parses as
``(a AND b) OR c`` and ``a b c`` as ``a AND b AND c``.

Implicit conjunction is inserted between any two operands with no
explicit operator: word/word, word/'(' , ')'/'(' , ')'/word, and after
NOT chains. A dangling operator (``a AND``, ``OR b``, ``a OR AND b``)
is an UNEXPECTED_TOKEN error anchored at the offending token.

An empty / whitespace-only query parses to :class:`MatchAll`.

Range syntax (space-tolerant)::

    [10 TO 20]   gte / lte
    {10 TO 20}   gt  / lt
    [* TO 20]    open lower bound
    [10 TO *]    open upper bound
    Field-qualified:  year:[2000 TO 2020]
"""

from __future__ import annotations

from typing import Optional

from searchdsl.astnodes import (
    And,
    MatchAll,
    Node,
    Not,
    Or,
    Phrase,
    Pos,
    Range,
    Term,
    first_pos,
)
from searchdsl.errors import ErrorLocation, EmptyQueryError, QuerySyntaxError
from searchdsl.lexer import Token, lex

_OPEN_BOUND = {"[": False, "{": True}  # bracket -> exclusive?
_CLOSE_BOUND = {"]": False, "}": True}


class Parser:
    def __init__(self, tokens: list[Token], text: str):
        self.toks = tokens
        self.text = text
        self.i = 0

    # -- token stream helpers -------------------------------------------

    def _peek(self, offset: int = 0) -> Optional[Token]:
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else None

    def _next(self) -> Optional[Token]:
        t = self._peek()
        if t is not None:
            self.i += 1
        return t

    def _error(self, code: str, message: str, tok: Optional[Token]):
        pos = ErrorLocation(tok.pos.start, tok.pos.end) if tok else ErrorLocation(
            len(self.text), len(self.text)
        )
        raise QuerySyntaxError(message, pos=pos, code=code)

    # -- grammar ---------------------------------------------------------

    def parse(self) -> Node:
        if not self.toks:
            raise EmptyQueryError(
                "query is empty or whitespace-only",
                pos=ErrorLocation(0, len(self.text)),
            )
        node = self._parse_or()
        leftover = self._peek()
        if leftover is not None:
            if leftover.kind == "RPAREN":
                self._error("UNBALANCED_PAREN", "unmatched ')'", leftover)
            self._error(
                "UNEXPECTED_TOKEN",
                f"unexpected token {leftover.value!r} after complete expression",
                leftover,
            )
        return node

    def _is_word(self, tok: Optional[Token], word: str) -> bool:
        return tok is not None and tok.kind == "TERM" and tok.value == word

    def _parse_or(self) -> Node:
        left = self._parse_and()
        children = [left]
        while self._is_word(self._peek(), "OR"):
            op = self._next()
            nxt = self._peek()
            if nxt is None:
                self._error("UNEXPECTED_TOKEN", "missing right operand after OR", op)
            right = self._parse_and()
            children.append(right)
        if len(children) == 1:
            return left
        return Or(tuple(children), pos=first_pos(children[0], children[-1]))

    def _parse_and(self) -> Node:
        left = self._parse_not()
        children = [left]
        while True:
            tok = self._peek()
            if self._is_word(tok, "AND"):
                self._next()
                right = self._parse_operand_after("AND", tok)
                children.append(right)
            elif self._is_word(tok, "OR"):
                break
            elif tok is not None and tok.kind == "RPAREN":
                # Let the enclosing group decide; top-level ')' is reported there.
                break
            elif tok is None:
                break
            else:
                # Implicit AND: any token that can start an operand.
                if tok.kind in ("RBRACK", "TO", "COLON"):
                    self._error(
                        "UNEXPECTED_TOKEN",
                        f"unexpected token {tok.value!r}",
                        tok,
                    )
                right = self._parse_not()
                children.append(right)
        if len(children) == 1:
            return left
        return And(tuple(children), pos=first_pos(children[0], children[-1]))

    def _parse_operand_after(self, op: str, op_tok: Token) -> Node:
        """Parse the RHS of an explicit binary operator, anchoring a missing
        operand on the operator token itself."""
        nxt = self._peek()
        if nxt is None:
            self._error(
                "UNEXPECTED_TOKEN",
                f"missing right operand after {op}",
                op_tok,
            )
        if self._is_word(nxt, "AND") or self._is_word(nxt, "OR"):
            self._error(
                "UNEXPECTED_TOKEN",
                f"{nxt.value} follows {op} without an operand",
                nxt,
            )
        if nxt.kind in ("RPAREN", "RBRACK", "TO", "COLON"):
            self._error(
                "UNEXPECTED_TOKEN",
                f"unexpected token {nxt.value!r} after {op}",
                nxt,
            )
        return self._parse_not()

    def _parse_not(self) -> Node:
        tok = self._peek()
        if self._is_word(tok, "NOT"):
            op = self._next()
            child = self._parse_not()
            return Not(child, pos=Pos(op.pos.start, child.pos.end if child.pos else op.pos.end))
        # NOT is a reserved word outside operator position.
        if self._is_word(tok, "NOT"):  # pragma: no cover - handled above
            self._error("UNEXPECTED_TOKEN", "NOT without an operand", tok)
        return self._parse_atom()

    def _parse_atom(self) -> Node:
        tok = self._peek()
        if tok is None:
            self._error(
                "UNEXPECTED_TOKEN",
                "expected a term, phrase or '(' but found end of query",
                None,
            )

        # AND/OR are reserved words: in operand position they are a syntax
        # error (so "a AND" points at AND, "OR a" points at OR).
        if self._is_word(tok, "AND") or self._is_word(tok, "OR"):
            self._error(
                "UNEXPECTED_TOKEN",
                f"{tok.value} is an operator but no operand surrounds it",
                tok,
            )

        if tok.kind == "LPAREN":
            return self._parse_group()

        if tok.kind in ("TERM", "PHRASE"):
            self._next()
            if tok.kind == "PHRASE":
                return Phrase(value=tok.value, pos=tok.pos)
            return Term(value=tok.value, pos=tok.pos)

        if tok.kind == "FIELD":
            return self._parse_field(tok)

        if tok.kind == "LBRACK":
            return self._parse_range()

        if tok.kind == "RPAREN":
            self._error("UNBALANCED_PAREN", "unmatched ')'", tok)
        self._error(
            "UNEXPECTED_TOKEN",
            f"expected a term, phrase or '(' but found {tok.value!r}",
            tok,
        )

    def _parse_group(self) -> Node:
        lp = self._next()  # '('
        # Empty parentheses "()" carry the empty-query semantics.
        if self._peek() is not None and self._peek().kind == "RPAREN":
            rp = self._next()
            return MatchAll(pos=Pos(lp.pos.start, rp.pos.end))
        inner = self._parse_or()
        rp = self._peek()
        if rp is None or rp.kind != "RPAREN":
            self._error("UNBALANCED_PAREN", "unmatched '('", lp)
        self._next()  # consume ')'
        return inner.with_pos(Pos(lp.pos.start, rp.pos.end))

    # -- fields and ranges ----------------------------------------------

    def _parse_field(self, field_tok: Token) -> Node:
        self._next()  # consume FIELD
        value_tok = self._peek()
        if value_tok is None:
            self._error(
                "UNEXPECTED_TOKEN",
                f"field {field_tok.name!r} is missing a value",
                field_tok,
            )
        if value_tok.kind == "LBRACK":
            rng = self._parse_range()
            return Range(
                field_name=field_tok.name,
                gte=rng.gte,
                gt=rng.gt,
                lte=rng.lte,
                lt=rng.lt,
                pos=Pos(field_tok.pos.start, rng.pos.end if rng.pos else field_tok.pos.end),
            )
        if value_tok.kind == "PHRASE":
            self._next()
            return Phrase(
                value_tok.value,
                field_name=field_tok.name,
                pos=Pos(field_tok.pos.start, value_tok.pos.end),
            )
        if value_tok.kind == "TERM":
            self._next()
            return Term(
                value_tok.value,
                field_name=field_tok.name,
                pos=Pos(field_tok.pos.start, value_tok.pos.end),
            )
        self._error(
            "UNEXPECTED_TOKEN",
            f"field {field_tok.name!r} must be followed by a word, quoted string or range",
            value_tok,
        )

    def _parse_bound(self, label: str) -> Optional[str]:
        tok = self._peek()
        if tok is None:
            self._error(
                "RANGE_MALFORMED",
                f"range is missing the {label} bound",
                tok,
            )
        if tok.kind == "TERM" and tok.value == "*":
            self._next()
            return None
        if tok.kind in ("TERM", "PHRASE"):
            self._next()
            return tok.value
        self._error(
            "RANGE_MALFORMED",
            f"range {label} bound must be a word, quoted value or '*'",
            tok,
        )

    def _parse_range(self) -> Range:
        lb = self._next()  # [ or {
        lower = self._parse_bound("lower")
        to_tok = self._peek()
        if not (to_tok is not None and to_tok.kind == "TO"):
            self._error(
                "RANGE_MALFORMED",
                "range bounds must be separated by TO",
                to_tok,
            )
        self._next()  # TO
        upper = self._parse_bound("upper")
        rb = self._peek()
        if rb is None or rb.kind != "RBRACK":
            self._error(
                "UNBALANCED_PAREN",
                "range is missing a closing ']' or '}'",
                lb,
            )
        self._next()
        if rb.value not in _CLOSE_BOUND:  # pragma: no cover - lexer guarantees
            self._error("RANGE_MALFORMED", "invalid range closing bracket", rb)
        lower_excl = _OPEN_BOUND[lb.value]
        upper_excl = _CLOSE_BOUND[rb.value]

        gte = gt = lte = lt = None
        if lower is not None:
            if lower_excl:
                gt = lower
            else:
                gte = lower
        if upper is not None:
            if upper_excl:
                lt = upper
            else:
                lte = upper
        if lower is None and upper is None:
            self._error("RANGE_EMPTY", "range [* TO *] matches everything; omit the range", lb)
        return Range(
            field_name="",
            gte=gte,
            gt=gt,
            lte=lte,
            lt=lt,
            pos=Pos(lb.pos.start, rb.pos.end),
        )


def parse(text: str) -> Node:
    """Lexer + parser. Empty/blank input raises QUERY_EMPTY."""
    tokens = lex(text)
    return Parser(tokens, text).parse()
