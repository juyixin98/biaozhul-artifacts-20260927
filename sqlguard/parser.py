"""Recursive-descent + Pratt parser for the supported SQL subset.

The parser consumes the token stream produced by :mod:`sqlguard.lexer` and
builds the AST in :mod:`sqlguard.ast_nodes`. Parsing is structural, not
textual: the analyzer therefore never mistakes a placeholder in a comment or
string for a bound parameter (the lexer already classified those tokens).

Anything understood lexically but outside the modeled surface (subqueries,
CTEs, UNION, window functions, triggers, DDL, ...) raises
:class:`UnsupportedSyntax`; malformed input raises :class:`ParseError`. The
kernel maps the former to an ``unanalyzable`` verdict with a precise reason
and the latter likewise — neither is silently accepted.
"""

from __future__ import annotations

from . import ast_nodes as ast
from .lexer import Token, TokenKind, tokenize, Span

# binary operator precedence (SQLite-ish)
_BINARY_PRECEDENCE = {
    "OR": 1,
    "AND": 2,
    "=": 4, "==": 4, "!=": 4, "<>": 4, ">": 4, "<": 4, ">=": 4, "<=": 4,
    "IS": 4, "IN": 4, "LIKE": 4, "GLOB": 4, "BETWEEN": 4,
    "|": 5, "&": 6, "<<": 7, ">>": 7,
    "+": 8, "-": 8, "*": 9, "/": 9, "%": 9, "||": 10,
}


class ParseError(Exception):
    def __init__(self, message: str, span: Span | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.span = span


class UnsupportedSyntax(ParseError):
    """Understood input, but outside the analyzable surface."""


def parse(sql: str) -> ast.Script:
    result = tokenize(sql)
    p = _Parser(result.tokens, result.inert_occurrences)
    return p.parse_script()


class _Parser:
    def __init__(self, tokens: list[Token], inert) -> None:
        # comments are attached to the script, not the grammar
        self.toks = [t for t in tokens if t.kind is not TokenKind.COMMENT]
        self.comments = [t.span for t in tokens if t.kind is TokenKind.COMMENT]
        self.quoted_idents = [
            t.span for t in tokens if t.kind is TokenKind.QUOTED_IDENT
        ]
        self.pos = 0

    # -- token utilities ---------------------------------------------------

    @property
    def cur(self) -> Token:
        return self.toks[self.pos]

    def _advance(self) -> Token:
        t = self.toks[self.pos]
        if t.kind is not TokenKind.EOF:
            self.pos += 1
        return t

    def _is_kw(self, *words: str) -> bool:
        t = self.cur
        return t.kind is TokenKind.KEYWORD and t.value in words

    def _eat_kw(self, word: str) -> Token | None:
        if self._is_kw(word):
            return self._advance()
        return None

    def _expect_kw(self, word: str) -> Token:
        if not self._eat_kw(word):
            t = self.cur
            raise ParseError(
                f"expected {word} but found {t.text!r}", t.span
            )
        return self.cur  # unreachable, keeps type checkers calm

    def _eat_punct(self, ch: str) -> Token | None:
        t = self.cur
        if t.kind is TokenKind.PUNCT and t.text == ch:
            return self._advance()
        return None

    def _expect_punct(self, ch: str) -> Token:
        if not self._eat_punct(ch):
            t = self.cur
            raise ParseError(f"expected {ch!r} but found {t.text!r}", t.span)
        return self.cur

    # -- script ------------------------------------------------------------

    def parse_script(self) -> ast.Script:
        statements: list[ast.Statement] = []
        while self.cur.kind is not TokenKind.EOF:
            statements.append(self.parse_statement())
            if self._eat_punct(";"):
                if self.cur.kind is not TokenKind.EOF:
                    # Stacked queries are a classic injection pattern and we
                    # only review one statement per request.
                    raise UnsupportedSyntax(
                        "multiple semicolon-separated statements are not "
                        "accepted",
                        self.cur.span,
                    )
            elif self.cur.kind is not TokenKind.EOF:
                raise ParseError(
                    "expected end of statement", self.cur.span
                )
        return ast.Script(statements, self.comments, self.quoted_idents)

    def parse_statement(self) -> ast.Statement:
        if self._is_kw("SELECT"):
            return self.parse_select()
        if self._is_kw("INSERT"):
            return self.parse_insert()
        if self._is_kw("UPDATE"):
            return self.parse_update()
        if self._is_kw("DELETE"):
            return self.parse_delete()
        t = self.cur
        if t.kind is TokenKind.EOF:
            raise ParseError("empty statement", t.span)
        if t.kind is TokenKind.KEYWORD:
            raise UnsupportedSyntax(
                f"statement starting with {t.value} is outside the supported "
                "subset (SELECT/INSERT/UPDATE/DELETE only)",
                t.span,
            )
        raise ParseError(
            f"statement cannot start with {t.text!r}", t.span
        )

    # -- SELECT ------------------------------------------------------------

    def parse_select(self) -> ast.Select:
        start = self._advance().span  # SELECT
        stmt = ast.Select(
            distinct=bool(self._eat_kw("DISTINCT"))
            or bool(self._eat_kw("ALL")),
        )
        self._parse_select_list(stmt)
        if self._eat_kw("FROM"):
            self._parse_from(stmt)
        if self._eat_kw("WHERE"):
            stmt.where = self.parse_expr()
        if self._eat_kw("GROUP"):
            self._expect_kw("BY")
            stmt.group_by.append(self.parse_expr())
            while self._eat_punct(","):
                stmt.group_by.append(self.parse_expr())
        if self._eat_kw("HAVING"):
            stmt.having = self.parse_expr()
        if self._eat_kw("ORDER"):
            self._expect_kw("BY")
            stmt.order_by.append(self._parse_order_item())
            while self._eat_punct(","):
                stmt.order_by.append(self._parse_order_item())
        stmt.limit, stmt.offset = self._parse_limit_offset()
        if self._is_kw("UNION", "INTERSECT", "EXCEPT"):
            raise UnsupportedSyntax(
                "set operations (UNION/INTERSECT/EXCEPT) are not supported",
                self.cur.span,
            )
        _ = start
        return stmt

    def _parse_select_list(self, stmt: ast.Select) -> None:
        while True:
            if self.cur.kind is TokenKind.OP and self.cur.text == "*":
                tok = self._advance()
                stmt.items.append(
                    (ast.ColumnRef("*", None, False, True, tok.span), None)
                )
            else:
                expr = self.parse_expr()
                alias = None
                if self._eat_kw("AS"):
                    alias = self._parse_alias_name()
                elif (self.cur.kind in (TokenKind.IDENT, TokenKind.QUOTED_IDENT)
                      and not self._looks_like_clause()):
                    alias = self._parse_alias_name()
                stmt.items.append((expr, alias))
            if not self._eat_punct(","):
                break

    def _looks_like_clause(self) -> bool:
        return self._is_kw(
            "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT",
            "UNION", "INTERSECT", "EXCEPT", "AS",
        )

    def _parse_alias_name(self) -> str:
        t = self._advance()
        if t.kind is TokenKind.IDENT:
            return t.text.upper()
        if t.kind is TokenKind.QUOTED_IDENT:
            return t.value
        raise ParseError(f"expected alias but found {t.text!r}", t.span)

    def _parse_from(self, stmt: ast.Select) -> None:
        stmt.from_tables.append(self._parse_table_ref())
        while True:
            join_type = ""
            if self._eat_kw("CROSS"):
                join_type = "CROSS"
                self._expect_kw("JOIN")
            elif self._eat_kw("INNER"):
                join_type = "INNER"
                self._expect_kw("JOIN")
            elif self._eat_kw("LEFT") or self._eat_kw("RIGHT"):
                join_type = "OUTER"
                self._eat_kw("OUTER")
                self._expect_kw("JOIN")
            elif self._eat_kw("JOIN"):
                join_type = "JOIN"
            else:
                break
            tbl = self._parse_table_ref()
            on = None
            if self._eat_kw("ON"):
                on = self.parse_expr()
            elif self._eat_kw("USING"):
                cols = self._parse_column_name_list()
                on = cols  # surfaced for completeness; analyzer handles joins
            stmt.joins.append((tbl, on))
            _ = join_type

    def _parse_table_ref(self) -> ast.TableRef:
        t = self.cur
        if t.kind is TokenKind.SLOT:
            self._advance()
            slot = ast.Slot(t.value, t.span)
            alias = self._table_alias()
            return ast.TableRef("", None, False, alias, t.span, slot=slot)
        if t.kind is TokenKind.PLACEHOLDER:
            # A value parameter standing where a table name belongs parses,
            # but the kernel rejects it: values can never name tables.
            self._advance()
            param = ast.Param(t.value, t.style or "?", t.span)
            alias = self._table_alias()
            return ast.TableRef("", None, False, alias, t.span, param=param)
        name, quoted, span = self._read_identifier()
        qualifier = None
        if self._eat_punct("."):
            qualifier = name
            name, quoted, span = self._read_identifier()
        alias = self._table_alias()
        return ast.TableRef(name, qualifier, quoted, alias, span)

    def _table_alias(self) -> str | None:
        if self._eat_kw("AS"):
            return self._parse_alias_name()
        if (self.cur.kind in (TokenKind.IDENT, TokenKind.QUOTED_IDENT)
                and not self._looks_like_clause()
                and not self._is_kw("JOIN", "ON", "USING", "INNER", "LEFT",
                                    "RIGHT", "CROSS", "WHERE", "GROUP",
                                    "ORDER", "HAVING", "LIMIT", "SET")):
            return self._parse_alias_name()
        return None

    def _read_identifier(self) -> tuple[str, bool, Span]:
        t = self.cur
        if t.kind is TokenKind.IDENT:
            self._advance()
            return t.text, False, t.span
        if t.kind is TokenKind.QUOTED_IDENT:
            self._advance()
            return t.value, True, t.span
        if t.kind is TokenKind.KEYWORD:
            # permit contextual keywords as identifiers in name positions
            self._advance()
            return t.value, False, t.span
        raise ParseError(f"expected identifier but found {t.text!r}", t.span)

    def _parse_column_name(self) -> ast.ColumnRef:
        t = self.cur
        if t.kind is TokenKind.SLOT:
            raise ParseError(
                "use an expression for column slots; ${...} handled in "
                "expression grammar",
                t.span,
            )
        first, quoted, span = self._read_identifier()
        qualifier = None
        if self._eat_punct("."):
            qualifier = first.upper() if not quoted else first
            if self._is_kw("*"):
                tok = self._advance()
                return ast.ColumnRef("*", qualifier, False, True, tok.span)
            first, quoted, span = self._read_identifier()
        return ast.ColumnRef(first, qualifier, quoted, False, span)

    def _parse_column_name_list(self) -> list[ast.ColumnRef]:
        self._expect_punct("(")
        cols = []
        while True:
            name, _q, span = self._read_identifier()
            cols.append(ast.ColumnRef(name, None, _q, False, span))
            if not self._eat_punct(","):
                break
        self._expect_punct(")")
        return cols

    def _parse_order_item(self) -> ast.OrderItem:
        expr = self.parse_expr()
        direction = None
        direction_slot = None
        if self._eat_kw("ASC"):
            direction = "ASC"
        elif self._eat_kw("DESC"):
            direction = "DESC"
        elif self.cur.kind is TokenKind.SLOT:
            tok = self._advance()
            direction_slot = ast.Slot(tok.value, tok.span)
        nulls = None
        if self._eat_kw("NULLS"):
            if self._eat_kw("FIRST"):
                nulls = "FIRST"
            elif self._eat_kw("LAST"):
                nulls = "LAST"
            else:
                raise ParseError("expected FIRST or LAST", self.cur.span)
        return ast.OrderItem(expr, direction, nulls,
                             direction_slot=direction_slot)

    def _parse_limit_offset(
        self,
    ) -> tuple[ast.Param | ast.Literal | None, ast.Param | ast.Literal | None]:
        if not self._eat_kw("LIMIT"):
            return None, None
        first = self._parse_limit_arg()
        if self._eat_punct(","):
            # SQLite legacy form: LIMIT offset, count
            second = self._parse_limit_arg()
            return second, first
        offset = None
        if self._eat_kw("OFFSET"):
            offset = self._parse_limit_arg()
        return first, offset

    def _parse_limit_arg(self) -> ast.Param | ast.Literal:
        t = self.cur
        if t.kind is TokenKind.PLACEHOLDER:
            self._advance()
            return ast.Param(t.value, t.style or "?", t.span, limit_context=True)
        if t.kind is TokenKind.NUMBER:
            self._advance()
            val: int | float = int(t.value) if t.value.isdigit() else float(t.value)
            return ast.Literal(val, t.span)
        if t.kind is TokenKind.KEYWORD and t.value == "NULL":
            raise ParseError("LIMIT cannot be NULL", t.span)
        raise ParseError(
            f"LIMIT/OFFSET expects a number or placeholder, got {t.text!r}",
            t.span,
        )

    # -- INSERT ------------------------------------------------------------

    def parse_insert(self) -> ast.Insert:
        self._advance()  # INSERT
        self._eat_kw("OR")
        if self._is_kw("ABORT", "FAIL", "IGNORE", "REPLACE", "ROLLBACK"):
            self._advance()
        self._expect_kw("INTO")
        stmt = ast.Insert()
        if self.cur.kind is TokenKind.SLOT:
            tok = self._advance()
            stmt.table = ast.TableRef(
                "", None, False, None, tok.span,
                slot=ast.Slot(tok.value, tok.span),
            )
        elif self.cur.kind is TokenKind.PLACEHOLDER:
            tok = self._advance()
            stmt.table = ast.TableRef(
                "", None, False, None, tok.span,
                param=ast.Param(tok.value, tok.style or "?", tok.span),
            )
        else:
            name, quoted, span = self._read_identifier()
            if self._eat_punct("."):
                q = name
                name, quoted, span = self._read_identifier()
            else:
                q = None
            stmt.table = ast.TableRef(name, q, quoted, None, span)
        if self._eat_punct("("):
            while True:
                cname, cquoted, cspan = self._read_identifier()
                stmt.columns.append(
                    ast.ColumnRef(cname, None, cquoted, False, cspan)
                )
                if not self._eat_punct(","):
                    break
            self._expect_punct(")")
        if self._eat_kw("VALUES"):
            while True:
                row = self._parse_value_row()
                stmt.rows.append(row)
                if not self._eat_punct(","):
                    break
        elif self._is_kw("SELECT"):
            stmt.from_select = self.parse_select()
        elif self._eat_kw("DEFAULT"):
            self._expect_kw("VALUES")
        else:
            raise ParseError(
                "INSERT requires VALUES or SELECT", self.cur.span
            )
        return stmt

    def _parse_value_row(self) -> list[ast.Expr]:
        self._expect_punct("(")
        row: list[ast.Expr] = []
        if not self._is_kw(")"):
            while True:
                row.append(self.parse_expr())
                if not self._eat_punct(","):
                    break
        self._expect_punct(")")
        return row

    # -- UPDATE / DELETE ---------------------------------------------------

    def parse_update(self) -> ast.Update:
        self._advance()  # UPDATE
        stmt = ast.Update()
        stmt.table = self._parse_table_ref()
        self._expect_kw("SET")
        while True:
            col = self._parse_column_name()
            if col.qualifier is not None:
                raise ParseError(
                    "qualified target columns are not supported in SET",
                    col.span,
                )
            if self.cur.kind not in (TokenKind.OP,) or self.cur.text != "=":
                raise ParseError("expected '=' in SET clause", self.cur.span)
            self._advance()
            value = self.parse_expr()
            stmt.assignments.append((col, value))
            if not self._eat_punct(","):
                break
        if self._eat_kw("WHERE"):
            stmt.where = self.parse_expr()
        return stmt

    def parse_delete(self) -> ast.Delete:
        self._advance()  # DELETE
        self._expect_kw("FROM")
        stmt = ast.Delete()
        stmt.table = self._parse_table_ref()
        if self._eat_kw("WHERE"):
            stmt.where = self.parse_expr()
        return stmt

    # -- expression grammar (Pratt) ----------------------------------------

    def parse_expr(self, min_prec: int = 0) -> ast.Expr:
        left = self._parse_prefix()
        while True:
            op, prec = self._peek_binary()
            if prec is None or prec < min_prec:
                break
            negated = op == "NOT_IN" or op == "NOT_BETWEEN"
            self._consume_binary()
            if op == "IS":
                left = self._parse_is_rest(left)
            elif op == "NOT_IN" or op == "IN":
                left = self._parse_in_rest(left, negated=negated)
            elif op == "BETWEEN" or op == "NOT_BETWEEN":
                low = self.parse_expr(_BINARY_PRECEDENCE["AND"] + 1)
                self._expect_kw("AND")
                high = self.parse_expr(_BINARY_PRECEDENCE["AND"] + 1)
                left = ast.Between(left, low, high, negated,
                                   _join_span(left, high))
            elif op in ("LIKE", "GLOB", "NOT_LIKE", "NOT_GLOB"):
                right = self.parse_expr(prec + 1)
                real_op = {"NOT_LIKE": "NOT LIKE", "NOT_GLOB": "NOT GLOB"}.get(op, op)
                left = ast.Binary(real_op, left, right, _join_span(left, right))
            else:
                right = self.parse_expr(prec + 1)
                left = ast.Binary(op, left, right, _join_span(left, right))
        return left

    def _peek_binary(self) -> tuple[str | None, int | None]:
        t = self.cur
        if t.kind is TokenKind.OP and t.text in _BINARY_PRECEDENCE:
            return t.text, _BINARY_PRECEDENCE[t.text]
        if t.kind is TokenKind.KEYWORD:
            if t.value == "NOT":
                nxt = self.toks[self.pos + 1]
                if nxt.kind is TokenKind.KEYWORD and nxt.value in (
                    "IN", "LIKE", "GLOB", "BETWEEN"
                ):
                    return "NOT_" + nxt.value, _BINARY_PRECEDENCE[nxt.value]
            if t.value in _BINARY_PRECEDENCE:
                return t.value, _BINARY_PRECEDENCE[t.value]
        return None, None

    def _consume_binary(self) -> None:
        t = self.cur
        self._advance()
        if t.kind is TokenKind.KEYWORD and t.value == "NOT":
            self._advance()  # consume IN/LIKE/GLOB/BETWEEN

    def _parse_is_rest(self, left: ast.Expr) -> ast.Expr:
        negated = bool(self._eat_kw("NOT"))
        self._expect_kw("NULL")
        return ast.IsNull(left, negated, _join_span(left, self.toks[self.pos - 1]))

    def _parse_in_rest(self, left: ast.Expr, *, negated: bool) -> ast.Expr:
        self._expect_punct("(")
        if self._is_kw("SELECT"):
            raise UnsupportedSyntax(
                "IN (SELECT ...) subqueries are not supported", self.cur.span
            )
        items: list[ast.Expr] = []
        is_tuple_row = False
        if self._eat_punct(")"):
            return ast.InList(left, [], negated, _join_span(left, self.toks[self.pos - 1]))
        # tuple form: (a,b) IN ((1,2),(3,4))
        if self._is_kw("("):
            is_tuple_row = True
        while True:
            if is_tuple_row:
                self._expect_punct("(")
                elems = []
                while True:
                    elems.append(self.parse_expr())
                    if not self._eat_punct(","):
                        break
                self._expect_punct(")")
                for e in elems:
                    if isinstance(e, ast.Param):
                        items.append(_with(e, tuple_context=True))
                    else:
                        items.append(e)
            else:
                items.append(self.parse_expr())
            if not self._eat_punct(","):
                break
        close = self._expect_punct_span()
        node = ast.InList(left, items, negated, _join_span(left, close))
        if not is_tuple_row and len(items) == 1 and isinstance(items[0], ast.Param):
            p = items[0]
            node.items[0] = _with(p, array_context=True)
        return node

    def _expect_punct_span(self):
        t = self.cur
        self._expect_punct(")")
        return t

    def _parse_prefix(self) -> ast.Expr:
        t = self.cur
        if t.kind is TokenKind.PLACEHOLDER:
            self._advance()
            return ast.Param(t.value, t.style or "?", t.span)
        if t.kind is TokenKind.SLOT:
            self._advance()
            return ast.Slot(t.value, t.span)
        if t.kind is TokenKind.STRING:
            self._advance()
            return ast.Literal(t.value, t.span)
        if t.kind is TokenKind.NUMBER:
            self._advance()
            val = int(t.value) if t.value.isdigit() else float(t.value)
            return ast.Literal(val, t.span)
        if t.kind is TokenKind.KEYWORD:
            if t.value in ("NULL", "TRUE", "FALSE"):
                self._advance()
                val = None if t.value == "NULL" else (t.value == "TRUE")
                return ast.Literal(val, t.span)
            if t.value == "NOT":
                self._advance()
                operand = self.parse_expr(_BINARY_PRECEDENCE["AND"] + 1)
                return ast.Unary("NOT", operand, t.span)
            if t.value in ("-", "+"):
                pass
            if t.value == "CASE":
                return self._parse_case()
            if t.value == "CAST":
                return self._parse_cast()
            if t.value in ("ANY", "SOME"):
                return self._parse_any(t)
            if t.value == "EXISTS":
                raise UnsupportedSyntax(
                    "EXISTS subqueries are not supported", t.span
                )
            if t.value == "DISTINCT":
                raise ParseError("unexpected DISTINCT", t.span)
        if t.kind is TokenKind.OP and t.text in "-+":
            self._advance()
            operand = self.parse_expr(9)
            if isinstance(operand, ast.Literal) and isinstance(operand.value, (int, float)):
                return ast.Literal(-operand.value if t.text == "-" else operand.value,
                                   t.span)
            return ast.Unary(t.text, operand, t.span)
        if t.kind is TokenKind.PUNCT and t.text == "(":
            # parenthesized expression; a SELECT inside is a subquery
            save = self.pos
            self._advance()
            if self._is_kw("SELECT"):
                raise UnsupportedSyntax(
                    "subqueries are outside the supported subset",
                    self.toks[save].span,
                )
            inner = self.parse_expr()
            self._expect_punct(")")
            return inner
        if t.kind is TokenKind.OP and t.text == "*":
            # star only valid in select-list, handled there
            raise ParseError("unexpected '*'", t.span)
        # identifier: column ref, or function call
        if t.kind in (TokenKind.IDENT, TokenKind.QUOTED_IDENT, TokenKind.KEYWORD):
            return self._parse_ident_or_call()
        raise ParseError(f"unexpected token {t.text!r}", t.span)

    def _parse_ident_or_call(self) -> ast.Expr:
        first, quoted, span = self._read_identifier()
        if self._eat_punct("."):
            qualifier = first.upper() if not quoted else first
            second_tok = self.cur
            if self._is_kw("*"):
                star = self._advance()
                return ast.ColumnRef("*", qualifier, False, True, star.span)
            if second_tok.kind is TokenKind.SLOT:
                self._advance()
                return ast.Slot(second_tok.value, second_tok.span,
                                qualifier=qualifier)
            second, squoted, sspan = self._read_identifier()
            ref = ast.ColumnRef(second, qualifier, squoted, False, sspan)
            return self._maybe_call(ref, span)
        ref = ast.ColumnRef(first, None, quoted, False, span)
        return self._maybe_call(ref, span)

    def _maybe_call(self, ref: ast.ColumnRef, start_span: Span) -> ast.Expr:
        if not self._eat_punct("("):
            return ref
        distinct = bool(self._eat_kw("DISTINCT"))
        star = False
        args: list[ast.Expr] = []
        if self.cur.kind is TokenKind.OP and self.cur.text == "*":
            star_tok = self._advance()
            star = True
            _ = star_tok
        elif not self._is_kw(")"):
            while True:
                if self._is_kw("SELECT") or self._is_kw("("):
                    nxt = self.cur
                    if nxt.kind is TokenKind.KEYWORD and nxt.value == "SELECT":
                        raise UnsupportedSyntax(
                            "subqueries in function arguments are not "
                            "supported", nxt.span
                        )
                args.append(self.parse_expr())
                if not self._eat_punct(","):
                    break
        close = self.cur
        self._expect_punct(")")
        name = ref.name.upper() if not ref.quoted else ref.name
        return ast.FuncCall(name, args, star, distinct,
                            Span(start_span.start, close.span.end,
                                 start_span.line, start_span.col))

    def _parse_case(self) -> ast.Expr:
        start = self._advance()  # CASE
        subject = None
        if not self._is_kw("WHEN"):
            subject = self.parse_expr()
        whens: list[tuple[ast.Expr, ast.Expr]] = []
        while self._eat_kw("WHEN"):
            cond = self.parse_expr()
            self._expect_kw("THEN")
            result = self.parse_expr()
            whens.append((cond, result))
        default = None
        if self._eat_kw("ELSE"):
            default = self.parse_expr()
        self._expect_kw("END")
        return ast.CaseExpr(subject, whens, default, start.span)

    def _parse_cast(self) -> ast.Expr:
        start = self._advance()  # CAST
        self._expect_punct("(")
        expr = self.parse_expr()
        self._expect_kw("AS")
        type_parts = [self._read_identifier()[0]]
        while self.cur.kind in (TokenKind.IDENT, TokenKind.KEYWORD):
            type_parts.append(self._advance().text)
        if self._eat_punct("("):
            while self.cur.kind is not TokenKind.PUNCT:
                self._advance()
            self._expect_punct(")")
        self._expect_punct(")")
        return ast.Cast(expr, " ".join(type_parts).upper(), start.span)

    def _parse_any(self, kw_tok: Token) -> ast.Expr:
        self._advance()  # ANY / SOME
        self._expect_punct("(")
        inner = self.parse_expr()
        self._expect_punct(")")
        if isinstance(inner, ast.Param):
            inner = _with(inner, array_context=True)
        return ast.FuncCall(kw_tok.value, [inner], False, False, kw_tok.span)


# helpers

def _join_span(left: ast.Expr, right) -> Span:
    ls = _expr_span(left)
    rs = right if isinstance(right, Span) else _expr_span(right)
    return Span(ls.start, rs.end, ls.line, ls.col)


def _expr_span(e) -> Span:
    if hasattr(e, "span"):
        return e.span
    raise TypeError(f"no span on {e!r}")


def _with(p: ast.Param, *, array_context=False, tuple_context=False,
          limit_context=False) -> ast.Param:
    return ast.Param(
        p.ref, p.style, p.span,
        array_context=array_context or p.array_context,
        tuple_context=tuple_context or p.tuple_context,
        limit_context=limit_context or p.limit_context,
    )
