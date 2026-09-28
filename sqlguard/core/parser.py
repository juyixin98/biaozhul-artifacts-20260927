"""Structural parser for the review DML subset.

The parser does not pretend to be a complete SQLite grammar implementation:
its job is to recover exactly the structure the security rules need —

* statement type and target relation(s);
* position of every bind parameter (value context vs identifier context);
* position of every ``{{ slot }}`` and its surrounding clause;
* WHERE presence for UPDATE/DELETE;
* regions it could not parse (reported honestly instead of guessed about).

Everything else (full expression typing, window frames, pragma internals) is
deliberately out of scope; unsupported regions become PARSE_REGION_UNANALYZABLE.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import Token, TokenKind, tokenize, LexError
from .models import (
    Finding,
    ParamUse,
    RelationRef,
    SlotUse,
    Span,
    StaticColumn,
    Statement,
)


# Keywords that terminate a value-expression scan at parenthesis depth 0.
_EXPR_BOUNDARY = frozenset({
    "ON", "USING", "WHERE", "GROUP", "HAVING", "WINDOW", "UNION", "EXCEPT",
    "INTERSECT", "ORDER", "LIMIT", "OFFSET", "RETURNING", "SET", "FROM",
})
_STMT_CLAUSE_KEYWORDS = _EXPR_BOUNDARY | frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})

# Keywords that continue the FROM clause; an ON expression must stop before them
_JOIN_CONTINUATION = frozenset(
    {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "OUTER"})
_ON_BOUNDARY = _EXPR_BOUNDARY | _JOIN_CONTINUATION


@dataclass
class ParseResult:
    statements: list[Statement] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


class Cursor:
    """Token stream with comment tokens filtered out (strings are kept)."""

    def __init__(self, tokens: list[Token]):
        self.toks = [t for t in tokens if t.kind not in (
            TokenKind.LINE_COMMENT, TokenKind.BLOCK_COMMENT)]
        self.i = 0

    @property
    def cur(self) -> Token:
        return self.toks[self.i]

    def peek(self, k: int = 0) -> Token:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def advance(self) -> Token:
        t = self.toks[self.i]
        if t.kind is not TokenKind.EOF:
            self.i += 1
        return t

    def at_kw(self, *names: str) -> bool:
        t = self.cur
        return t.kind is TokenKind.KEYWORD and t.value in {n.upper() for n in names}

    def eat_kw(self, *names: str) -> bool:
        if self.at_kw(*names):
            self.i += 1
            return True
        return False

    def at_punct(self, ch: str) -> bool:
        return self.cur.kind is TokenKind.PUNCT and self.cur.value == ch

    def eat_punct(self, ch: str) -> bool:
        if self.at_punct(ch):
            self.i += 1
            return True
        return False

    def at_eof(self) -> bool:
        return self.cur.kind is TokenKind.EOF


class Parser:
    def __init__(self) -> None:
        self.param_counter = 0
        self.slot_counter = 0
        self.findings: list[Finding] = []
        # uses collected while parsing WITH ... CTE bodies
        self._cte_params: list[ParamUse] = []
        self._cte_slots: list[SlotUse] = []
        self._cte_relations: list[RelationRef] = []

    # ---------- finding helpers ----------

    def _finding(self, code: str, tok: Token | None = None, context: str | None = None,
                 message_extra: str = "", **detail: object) -> None:
        from .models import FINDING_CATALOGUE
        msg = FINDING_CATALOGUE[code][1] + (f": {message_extra}" if message_extra else "")
        self.findings.append(
            Finding(code, msg, Span.from_token(tok) if tok else None, context, dict(detail))
        )

    # ---------- parameter / slot recording ----------

    def _record_param(self, tok: Token, context: str, expansion: bool = False) -> ParamUse:
        use = ParamUse(tok.value, self.param_counter, Span.from_token(tok), context, expansion)
        self.param_counter += 1
        return use

    def _record_slot(self, tok: Token, context: str) -> SlotUse:
        use = SlotUse(tok.value, self.slot_counter, Span.from_token(tok), context)
        self.slot_counter += 1
        return use

    # ---------- expression scanning ----------

    def _scan_expression(self, cur: Cursor, context: str, stmt: Statement,
                         stop_kws: frozenset[str] = _EXPR_BOUNDARY) -> None:
        """Consume a value-expression tuple, recording params/slots found.

        At depth 0 the scan stops on a clause-boundary keyword. Parenthesized
        groups are classified up front as subquery / IN-tuple / plain group.
        """
        depth = 0
        in_paren_is_value_tuple: list[bool] = []

        while not cur.at_eof():
            t = cur.cur
            if depth == 0 and t.kind is TokenKind.KEYWORD and t.value in stop_kws:
                return

            if t.kind is TokenKind.PUNCT:
                if t.value == "(":
                    # look back for IN/NOT, look ahead for SELECT
                    prev = cur.peek(-1)
                    follows_in = prev.kind is TokenKind.KEYWORD and prev.value == "IN"
                    inner = cur.peek(1)
                    is_subquery = inner.kind is TokenKind.KEYWORD and inner.value == "SELECT"
                    cur.advance()
                    depth += 1
                    in_paren_is_value_tuple.append(follows_in and not is_subquery)
                    if is_subquery:
                        # nested statement: parse the SELECT, params belong to it
                        self._parse_select_body(cur, stmt)
                        # consume matching ')' — _parse_select_body stops before it
                        cur.eat_punct(")")
                        depth -= 1
                        in_paren_is_value_tuple.pop()
                    continue
                if t.value == ")":
                    if depth == 0:
                        return
                    cur.advance()
                    depth -= 1
                    in_paren_is_value_tuple.pop()
                    continue
                if t.value == "," or t.value == ";":
                    if t.value == ";" and depth == 0:
                        return
                    cur.advance()
                    continue
                if t.value == ".":
                    cur.advance()
                    continue

            if t.kind is TokenKind.BIND_PARAM:
                cur.advance()
                stmt.params.append(
                    self._record_param(t, context, expansion=bool(in_paren_is_value_tuple)))
                continue
            if t.kind is TokenKind.SLOT:
                # slots are legal as identifier-atoms inside expressions
                cur.advance()
                stmt.slots.append(self._record_slot(t, context))
                continue

            cur.advance()

    # ---------- relation parsing (FROM / JOIN / UPDATE / INSERT targets) ----------

    def _parse_relation(self, cur: Cursor, context: str, stmt: Statement,
                        ctes: set[str], allow_function: bool = True) -> RelationRef:
        t = cur.cur

        if t.kind is TokenKind.BIND_PARAM:
            cur.advance()
            rel = RelationRef(
                name=None, slot_name=None, kind="param",
                span=Span.from_token(t),
                note="value bind-parameter used as relation name",
            )
            self._finding(
                "VALUE_PARAM_AS_IDENTIFIER", t, context,
                message_extra=f"{t.value} appears where a table name is required")
            stmt.params.append(self._record_param(t, f"{context}_relation"))
            self._skip_alias_hint(cur)
            return rel

        if t.kind is TokenKind.SLOT:
            cur.advance()
            rel = RelationRef(
                name=None, slot_name=t.value, kind="table",
                span=Span.from_token(t),
            )
            stmt.slots.append(self._record_slot(t, f"{context}_relation"))
            self._skip_alias_hint(cur)
            return rel

        if t.kind is TokenKind.PUNCT and t.value == "(":
            cur.advance()
            inner = cur.cur
            if inner.kind is TokenKind.KEYWORD and inner.value == "SELECT":
                self._parse_select_body(cur, stmt)
                cur.eat_punct(")")
                rel = RelationRef(name=None, slot_name=None, kind="subquery",
                                  span=Span.from_token(t))
                self._consume_alias(cur, rel, allow_as=True)
                return rel
            # parenthesised join group
            rel = self._parse_join_chain(cur, context, stmt, ctes, stop_paren=True)
            cur.eat_punct(")")
            self._consume_alias(cur, rel, allow_as=True)
            return rel

        # static (possibly qualified) name: a [. b [. c]]
        components: list[str] = []
        start_tok = t
        if t.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
            components.append(t.value)
            cur.advance()
            while cur.at_punct("."):
                cur.advance()
                nt = cur.cur
                if nt.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
                    components.append(nt.value)
                    cur.advance()
                else:
                    self._finding(
                        "PARSE_REGION_UNANALYZABLE", nt, context,
                        message_extra="malformed qualified name")
                    break
        else:
            self._finding(
                "PARSE_REGION_UNANALYZABLE", t, context,
                message_extra=f"unexpected {t.kind.value} where a relation is expected")
            cur.advance()
            rel = RelationRef(name=None, slot_name=None, kind="subquery",
                              span=Span.from_token(t), note="unparsed relation token")
            self._skip_alias_hint(cur)
            return rel

        last = components[-1] if components else None
        kind = "cte" if last in ctes else "table"
        rel = RelationRef(
            name=last, slot_name=None, kind=kind, span=Span.from_token(start_tok),
            components=tuple(components),
        )
        self._consume_alias(cur, rel, allow_as=True)
        # table-valued function: name(...) (but never for DML targets, where
        # the following parenthesis is the column list)
        if allow_function and cur.at_punct("("):
            cur.advance()
            self._scan_function_args(cur, context, stmt)
            rel.kind = "table_function"
        # INDEXED BY / NOT INDEXED hint
        if cur.at_kw("INDEXED"):
            cur.advance()
            cur.eat_kw("BY")
            if cur.cur.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
                cur.advance()
        elif cur.at_kw("NOT") and cur.peek(1).kind is TokenKind.KEYWORD and \
                cur.peek(1).value == "INDEXED":
            cur.advance(); cur.advance()
        return rel

    def _scan_function_args(self, cur: Cursor, context: str, stmt: Statement) -> None:
        depth = 1
        while depth > 0 and not cur.at_eof():
            t = cur.cur
            if t.kind is TokenKind.PUNCT and t.value == "(":
                depth += 1
            elif t.kind is TokenKind.PUNCT and t.value == ")":
                depth -= 1
                if depth == 0:
                    cur.advance()
                    return
            elif t.kind is TokenKind.BIND_PARAM:
                stmt.params.append(self._record_param(t, f"{context}_funcarg"))
            elif t.kind is TokenKind.SLOT:
                stmt.slots.append(self._record_slot(t, f"{context}_funcarg"))
            cur.advance()

    def _consume_alias(self, cur: Cursor, rel: RelationRef, allow_as: bool) -> None:
        if cur.eat_kw("AS"):
            if cur.cur.kind in (TokenKind.WORD, TokenKind.IDENTIFIER, TokenKind.STRING):
                rel.alias = cur.advance().value
        elif cur.cur.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
            # bare alias (avoid swallowing the next JOIN)
            rel.alias = cur.advance().value

    def _skip_alias_hint(self, cur: Cursor) -> None:
        dummy = RelationRef(
            name=None, slot_name=None, kind="table", span=Span(0, 0, 0, 0))
        self._consume_alias(cur, dummy, allow_as=True)

    def _parse_from(self, cur: Cursor, stmt: Statement, ctes: set[str]) -> None:
        if not cur.eat_kw("FROM"):
            return
        first = self._parse_join_chain(cur, "from", stmt, ctes)
        stmt.relations.append(first)

    def _parse_join_chain(self, cur: Cursor, context: str, stmt: Statement,
                          ctes: set[str], stop_paren: bool = False) -> RelationRef:
        first = self._parse_relation(cur, context, stmt, ctes)
        join_kws = ("JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "OUTER")
        while True:
            if stop_paren and cur.at_punct(")"):
                return first
            if cur.at_kw(","):
                cur.advance()
                stmt.relations.append(
                    self._parse_join_chain(cur, context, stmt, ctes, stop_paren=stop_paren))
                continue
            t = cur.cur
            if t.kind is TokenKind.KEYWORD and t.value in {
                    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "OUTER"}:
                # consume join qualifier words
                while cur.at_kw(*join_kws):
                    cur.advance()
                rhs = self._parse_relation(cur, "join", stmt, ctes)
                stmt.relations.append(rhs)
                if cur.eat_kw("ON"):
                    self._scan_expression(cur, "on", stmt, _ON_BOUNDARY)
                elif cur.eat_kw("USING"):
                    self._consume_name_list(cur, "join_using", stmt)
                continue
            break
        return first

    def _consume_name_list(self, cur: Cursor, context: str, stmt: Statement) -> None:
        if not cur.eat_punct("("):
            return
        while True:
            t = cur.cur
            if t.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
                cur.advance()
            elif t.kind is TokenKind.SLOT:
                cur.advance()
                stmt.slots.append(self._record_slot(t, context))
            elif t.kind is TokenKind.BIND_PARAM:
                cur.advance()
                self._finding("VALUE_PARAM_AS_IDENTIFIER", t, context,
                              message_extra="? cannot name a JOIN column")
                stmt.params.append(self._record_param(t, context))
            if not cur.eat_punct(","):
                break
        cur.eat_punct(")")

    # ---------- SELECT ----------

    def _parse_select_list(self, cur: Cursor, stmt: Statement) -> None:
        while True:
            self._scan_expression(cur, "select", stmt,
                                  _EXPR_BOUNDARY | {"FROM"})
            if not cur.eat_punct(","):
                break

    def _parse_group_or_order(self, cur: Cursor, name: str, stmt: Statement) -> None:
        cur.advance()  # GROUP / ORDER
        cur.eat_kw("BY")
        while True:
            # key: identifier chain | slot | literal | number | (expr)
            t = cur.cur
            if t.kind is TokenKind.BIND_PARAM:
                cur.advance()
                self._finding(
                    "VALUE_PARAM_AS_IDENTIFIER", t, name,
                    message_extra=(
                        f"{t.value} as a sort/group key cannot carry identifier "
                        f"semantics; use a {{{{ slot }}}} with a whitelist instead"))
                stmt.params.append(self._record_param(t, name))
            else:
                self._scan_expression(cur, name, stmt,
                                      _EXPR_BOUNDARY - {name.upper()})
            # ASC/DESC/COLLATE/NULLS FIRST|LAST tails
            while True:
                if cur.at_kw("ASC", "DESC"):
                    cur.advance()
                elif cur.eat_kw("COLLATE"):
                    if cur.cur.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
                        cur.advance()
                elif cur.eat_kw("NULLS"):
                    cur.eat_kw("FIRST", "LAST")
                else:
                    break
            if not cur.eat_punct(","):
                break

    def _parse_select_body(self, cur: Cursor, stmt: Statement, ctes: set[str] | None = None,
                           is_root: bool = False) -> None:
        ctes = ctes if ctes is not None else set()
        cur.eat_kw("DISTINCT", "ALL")
        self._parse_select_list(cur, stmt)
        self._parse_from(cur, stmt, ctes)
        if cur.eat_kw("WHERE"):
            stmt.has_where = True
            self._scan_expression(cur, "where", stmt, _EXPR_BOUNDARY - {"WHERE"})
        if cur.at_kw("GROUP"):
            self._parse_group_or_order(cur, "group", stmt)
        if cur.eat_kw("HAVING"):
            self._scan_expression(cur, "having", stmt, _EXPR_BOUNDARY - {"HAVING"})
        if cur.eat_kw("WINDOW"):
            # skip window name + definition until a boundary keyword
            cur.cur  # noqa: B018 - cheap no-op to keep the read explicit
            self._scan_expression(cur, "window", stmt)
        if cur.at_kw("ORDER"):
            self._parse_group_or_order(cur, "order", stmt)
        if cur.eat_kw("LIMIT"):
            self._scan_expression(cur, "limit", stmt,
                                  (_EXPR_BOUNDARY - {"LIMIT", "OFFSET"}) | {"OFFSET"})
            if cur.eat_kw("OFFSET"):
                self._scan_expression(cur, "offset", stmt, _EXPR_BOUNDARY - {"OFFSET"})

    def _parse_select(self, cur: Cursor, ctes: set[str]) -> Statement:
        stmt = Statement("SELECT")
        if not cur.eat_kw("SELECT"):
            raise AssertionError("SELECT expected")
        self._parse_select_body(cur, stmt, ctes)
        # compound SELECT ...
        while cur.at_kw("UNION", "INTERSECT", "EXCEPT"):
            stmt.compound = True
            cur.advance()
            cur.eat_kw("ALL", "DISTINCT")
            if cur.eat_kw("SELECT"):
                self._parse_select_body(cur, stmt, ctes)
            else:
                tok = cur.cur
                self._finding("PARSE_REGION_UNANALYZABLE", tok, "compound",
                              message_extra="expected SELECT after set operator")
                break
        self._parse_returning(cur, stmt)
        return stmt

    def _parse_returning(self, cur: Cursor, stmt: Statement) -> None:
        if cur.eat_kw("RETURNING"):
            self._scan_expression(cur, "returning", stmt,
                                  _EXPR_BOUNDARY - {"RETURNING"})

    # ---------- INSERT ----------

    def _parse_column_list(self, cur: Cursor, stmt: Statement, target: str | None) -> None:
        if not cur.eat_punct("("):
            return
        while True:
            t = cur.cur
            if t.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
                cur.advance()
                stmt.insert_columns.append(
                    StaticColumn(t.value, Span.from_token(t), "insert_col", target))
            elif t.kind is TokenKind.SLOT:
                cur.advance()
                stmt.slots.append(self._record_slot(t, "insert_col"))
            elif t.kind is TokenKind.BIND_PARAM:
                cur.advance()
                self._finding("VALUE_PARAM_AS_IDENTIFIER", t, "insert_col",
                              message_extra="? cannot name an INSERT column")
                stmt.params.append(self._record_param(t, "insert_col"))
            elif t.kind is TokenKind.PUNCT and t.value == ")":
                break
            else:
                self._finding("PARSE_REGION_UNANALYZABLE", t, "insert_col",
                              message_extra=f"unexpected {t.kind.value}")
                cur.advance()
            if not cur.eat_punct(","):
                break
        cur.eat_punct(")")

    def _parse_values_tuples(self, cur: Cursor, stmt: Statement) -> None:
        while True:
            if not cur.eat_punct("("):
                tok = cur.cur
                self._finding("PARSE_REGION_UNANALYZABLE", tok, "values",
                              message_extra="expected VALUES tuple")
                break
            first = True
            while not cur.at_eof():
                t = cur.cur
                if t.kind is TokenKind.PUNCT and t.value == ")":
                    cur.advance()
                    break
                if not first:
                    if t.kind is TokenKind.PUNCT and t.value == ",":
                        cur.advance()
                        t = cur.cur
                    else:
                        self._finding("PARSE_REGION_UNANALYZABLE", t, "values",
                                      message_extra="expected ',' in VALUES tuple")
                first = False
                if t.kind is TokenKind.BIND_PARAM:
                    cur.advance()
                    stmt.params.append(self._record_param(t, "values"))
                elif t.kind is TokenKind.SLOT:
                    cur.advance()
                    stmt.slots.append(self._record_slot(t, "values"))
                else:
                    # literal/expression element: scan one element
                    self._scan_one_expr_element(cur, "values", stmt)
            if cur.eat_punct(","):
                continue
            break

    def _scan_one_expr_element(self, cur: Cursor, context: str, stmt: Statement) -> None:
        depth = 0
        while not cur.at_eof():
            t = cur.cur
            if depth == 0 and (
                    (t.kind is TokenKind.PUNCT and t.value in (",", ")"))
                    or (t.kind is TokenKind.KEYWORD and t.value in _EXPR_BOUNDARY)):
                return
            if t.kind is TokenKind.PUNCT and t.value == "(":
                depth += 1
            elif t.kind is TokenKind.PUNCT and t.value == ")":
                depth -= 1
            elif t.kind is TokenKind.BIND_PARAM:
                stmt.params.append(self._record_param(t, context))
            elif t.kind is TokenKind.SLOT:
                stmt.slots.append(self._record_slot(t, context))
            cur.advance()

    def _parse_insert(self, cur: Cursor, ctes: set[str]) -> Statement:
        stmt = Statement("INSERT")
        # INSERT [OR REPLACE|ABORT|...] INTO
        if cur.at_kw("REPLACE"):
            cur.advance()
            stmt.stmt_type = "INSERT"
        else:
            cur.advance()  # INSERT
            if cur.eat_kw("OR"):
                cur.eat_kw("REPLACE", "ABORT", "FAIL", "IGNORE", "ROLLBACK")
        cur.eat_kw("INTO")
        target = self._parse_relation(cur, "insert_target", stmt, ctes, allow_function=False)
        stmt.target = target
        self._parse_column_list(cur, stmt, target.name)

        if cur.eat_kw("DEFAULT"):
            cur.eat_kw("VALUES")
        elif cur.eat_kw("VALUES"):
            self._parse_values_tuples(cur, stmt)
        elif cur.eat_kw("SELECT"):
            self._parse_select_body(cur, stmt, ctes)
        else:
            tok = cur.cur
            self._finding("PARSE_REGION_UNANALYZABLE", tok, "insert",
                          message_extra="expected VALUES or SELECT")
        # SQLite upsert: ON CONFLICT(...) DO ...
        if cur.eat_kw("ON") and cur.eat_kw("CONFLICT"):
            self._consume_name_list(cur, "conflict", stmt)
            if cur.eat_kw("DO"):
                if cur.eat_kw("NOTHING"):
                    pass
                elif cur.eat_kw("UPDATE"):
                    cur.eat_kw("SET")
                    self._parse_set_list(cur, stmt, target.name,
                                         stop=_EXPR_BOUNDARY | {"WHERE"})
                    if cur.eat_kw("WHERE"):
                        self._scan_expression(cur, "where", stmt)
        self._parse_returning(cur, stmt)
        return stmt

    # ---------- UPDATE ----------

    def _parse_set_list(self, cur: Cursor, stmt: Statement, target: str | None,
                        stop: frozenset[str]) -> None:
        while True:
            t = cur.cur
            # LHS: ( col [, col] ) | col [.col]
            if t.kind is TokenKind.BIND_PARAM:
                cur.advance()
                self._finding("VALUE_PARAM_AS_IDENTIFIER", t, "set_lhs",
                              message_extra="? cannot name an UPDATE target column")
                stmt.params.append(self._record_param(t, "set_lhs"))
                # consume until '='
                while not cur.at_eof() and not (
                        cur.cur.kind is TokenKind.OP and cur.cur.value == "=") \
                        and cur.cur.kind is not TokenKind.KEYWORD:
                    cur.advance()
            elif cur.at_punct("("):
                cur.advance()
                while True:
                    ct = cur.cur
                    if ct.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
                        cur.advance()
                        stmt.set_columns.append(
                            StaticColumn(ct.value, Span.from_token(ct), "set_lhs", target))
                    elif ct.kind is TokenKind.SLOT:
                        cur.advance()
                        stmt.slots.append(self._record_slot(ct, "set_lhs"))
                    elif ct.kind is TokenKind.BIND_PARAM:
                        cur.advance()
                        self._finding("VALUE_PARAM_AS_IDENTIFIER", ct, "set_lhs",
                                      message_extra="? cannot name an UPDATE column")
                        stmt.params.append(self._record_param(ct, "set_lhs"))
                    if not cur.eat_punct(","):
                        break
                cur.eat_punct(")")
            else:
                # possibly qualified column name
                parts: list[str] = []
                start_tok = t
                if t.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
                    parts.append(t.value)
                    cur.advance()
                    while cur.at_punct("."):
                        cur.advance()
                        nt = cur.cur
                        if nt.kind in (TokenKind.WORD, TokenKind.IDENTIFIER):
                            parts.append(nt.value)
                            cur.advance()
                        else:
                            break
                    stmt.set_columns.append(
                        StaticColumn(parts[-1], Span.from_token(start_tok),
                                     "set_lhs", target))
                elif t.kind is TokenKind.SLOT:
                    cur.advance()
                    stmt.slots.append(self._record_slot(t, "set_lhs"))
                else:
                    self._finding("PARSE_REGION_UNANALYZABLE", t, "set_lhs",
                                  message_extra=f"unexpected {t.kind.value}")
                    cur.advance()

            # '=' or assignment tuple '= (?, ?, ...)'
            if cur.cur.kind is TokenKind.OP and cur.cur.value in ("=", "=="):
                cur.advance()
                self._scan_one_expr_element(cur, "set_rhs", stmt)
            if not cur.eat_punct(","):
                break

    def _parse_update(self, cur: Cursor, ctes: set[str]) -> Statement:
        stmt = Statement("UPDATE")
        cur.advance()  # UPDATE
        target = self._parse_relation(cur, "update_target", stmt, ctes, allow_function=False)
        stmt.target = target
        if not cur.eat_kw("SET"):
            self._finding("PARSE_REGION_UNANALYZABLE", cur.cur, "update",
                          message_extra="expected SET")
        else:
            self._parse_set_list(cur, stmt, target.name, stop=_EXPR_BOUNDARY)
        if cur.at_kw("FROM"):
            cur.advance()
            extra = self._parse_join_chain(cur, "update_from", stmt, ctes)
            stmt.relations.append(extra)
        if cur.eat_kw("WHERE"):
            stmt.has_where = True
            self._scan_expression(cur, "where", stmt, _EXPR_BOUNDARY - {"WHERE"})
        if cur.at_kw("ORDER"):
            self._parse_group_or_order(cur, "order", stmt)
        if cur.eat_kw("LIMIT"):
            self._scan_expression(cur, "limit", stmt)
        self._parse_returning(cur, stmt)
        return stmt

    # ---------- DELETE ----------

    def _parse_delete(self, cur: Cursor, ctes: set[str]) -> Statement:
        stmt = Statement("DELETE")
        cur.advance()  # DELETE
        cur.eat_kw("FROM")
        target = self._parse_relation(cur, "delete_target", stmt, ctes, allow_function=False)
        stmt.target = target
        if cur.eat_kw("WHERE"):
            stmt.has_where = True
            self._scan_expression(cur, "where", stmt, _EXPR_BOUNDARY - {"WHERE"})
        if cur.at_kw("ORDER"):
            self._parse_group_or_order(cur, "order", stmt)
        if cur.eat_kw("LIMIT"):
            self._scan_expression(cur, "limit", stmt)
        self._parse_returning(cur, stmt)
        return stmt

    # ---------- WITH / top-level ----------

    def _parse_cte(self, cur: Cursor, ctes: set[str]) -> None:
        # name [(cols)] AS ( select )
        t = cur.cur
        if t.kind not in (TokenKind.WORD, TokenKind.IDENTIFIER):
            self._finding("PARSE_REGION_UNANALYZABLE", t, "with",
                          message_extra="expected CTE name")
            return
        ctes.add(t.value)
        cur.advance()
        if cur.eat_punct("("):
            while cur.cur.kind is not TokenKind.PUNCT and not cur.at_eof():
                cur.advance()
            cur.eat_punct(")")
        if not (cur.eat_kw("AS") and cur.eat_punct("(")):
            self._finding("PARSE_REGION_UNANALYZABLE", cur.cur, "with",
                          message_extra="expected AS ( after CTE name")
            return
        if cur.eat_kw("SELECT"):
            sub = Statement("SELECT")
            self._parse_select_body(cur, sub, ctes)
            cur.eat_punct(")")
            self._cte_params.extend(sub.params)
            self._cte_slots.extend(sub.slots)
            self._cte_relations.extend(sub.relations)
        else:
            cur.eat_punct(")")
            self._finding("PARSE_REGION_UNANALYZABLE", cur.cur, "with",
                          message_extra="only SELECT CTEs are supported")

    def parse(self, sql: str) -> ParseResult:
        result = ParseResult()
        try:
            tokens = tokenize(sql)
        except LexError as exc:
            result.findings.append(Finding(
                "LEX_ERROR", str(exc),
                Span(exc.offset, exc.offset + 1, exc.line, exc.col), "lex"))
            return result

        cur = Cursor(tokens)
        if cur.at_eof():
            self._finding("EMPTY_TEMPLATE")
            result.findings = self.findings
            return result

        # leading semicolons are harmless
        while cur.eat_punct(";"):
            pass
        if cur.at_eof():
            self._finding("EMPTY_TEMPLATE")
            result.findings = self.findings
            return result

        ctes: set[str] = set()
        if cur.eat_kw("WITH"):
            cur.eat_kw("RECURSIVE")
            while True:
                self._parse_cte(cur, ctes)
                if not cur.eat_punct(","):
                    break

        t = cur.cur
        if t.kind is TokenKind.KEYWORD and t.value == "SELECT":
            stmt = self._parse_select(cur, ctes)
        elif t.kind is TokenKind.KEYWORD and t.value in ("INSERT", "REPLACE"):
            stmt = self._parse_insert(cur, ctes)
        elif t.kind is TokenKind.KEYWORD and t.value == "UPDATE":
            stmt = self._parse_update(cur, ctes)
        elif t.kind is TokenKind.KEYWORD and t.value == "DELETE":
            stmt = self._parse_delete(cur, ctes)
        else:
            stmt = None
            if t.kind is TokenKind.KEYWORD:
                self._finding("STATEMENT_TYPE_NOT_ALLOWED", t,
                              message_extra=f"{t.value} statements are not reviewable")
            else:
                self._finding("UNRECOGNIZED_STATEMENT", t,
                              message_extra=f"starts with {t.value!r}")

        if stmt is not None:
            # bind uses inside CTE bodies participate in binding checks too
            stmt.params = self._cte_params + stmt.params
            stmt.slots = self._cte_slots + stmt.slots
            stmt.relations = self._cte_relations + stmt.relations
            # tolerate a single trailing semicolon
            cur.eat_punct(";")
            if not cur.at_eof():
                stmt.trailing_tokens = 1
                self._finding(
                    "MULTIPLE_STATEMENTS", cur.cur,
                    message_extra="text remains after the end of the single statement")
                # identify the second statement verb for a precise rejection
                t2 = cur.cur
                if t2.kind is TokenKind.KEYWORD:
                    self._finding(
                        "STATEMENT_TYPE_NOT_ALLOWED", t2,
                        message_extra=(
                            f"{t2.value} is a second statement; multi-statement "
                            "templates are not reviewable"))
            result.statements.append(stmt)

        result.findings = self.findings
        return result
