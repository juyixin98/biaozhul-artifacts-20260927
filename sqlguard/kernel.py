"""The security kernel.

Review = parse once, then walk the AST with the declared policy and the
read-only schema snapshot. The kernel never executes SQL; its only external
state is the immutable fixture catalog.

The rules implemented (see docs/SECURITY.md for rationale):

* only declared statement kinds and whitelisted tables/columns are accepted;
* a value placeholder appearing in an identifier position (table or column
  slot) is rejected as ``VALUE_USED_AS_IDENTIFIER``;
* an identifier slot ``${name}`` must be declared in policy, be supplied a
  binding, play a role (table/column/keyword) the declaration allows, and the
  bound identifier must match the declaration's whitelist;
* bindings are type-checked: scalars for value positions, arrays for
  ``IN (?)`` / ``ANY(?)`` array positions, non-negative integers for
  LIMIT/OFFSET;
* unknown/ambiguous columns and unresolved tables are rejected rather than
  guessed;
* anything the parser cannot model is reported as ``unanalyzable`` with the
  precise reason, never silently accepted.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable

from . import ast_nodes as ast
from .findings import (
    Finding, ReviewResult, Verdict, Severity,
)
from .lexer import LexError, tokenize
from .parser import parse, ParseError, UnsupportedSyntax
from .policy import Policy, SlotPolicy
from .isolation import SchemaSnapshot, SchemaUnavailable
from .redaction import (
    new_request_id, sql_digest, redact_value, redact_identifier,
)

_SCALAR_TYPES = (str, int, float, bool, type(None))


class Kernel:
    def __init__(self, policy: Policy, schema: SchemaSnapshot) -> None:
        self.policy = policy
        self.schema = schema

    # ------------------------------------------------------------------ #
    # entry point
    # ------------------------------------------------------------------ #

    def review(self, sql: str, *, parameters: dict | list | None = None,
               identifiers: dict | None = None, request_id: str | None = None
               ) -> ReviewResult:
        rid = request_id or new_request_id()
        digest = sql_digest(sql)
        findings: list[Finding] = []
        warnings: list[Finding] = []
        limitations: list[str] = []
        ctx = _Context(self.policy, self.schema, _normalize_bindings(parameters),
                       identifiers or {}, findings, warnings, limitations)

        verdict = Verdict.ACCEPT.value
        try:
            script = parse(sql)
        except UnsupportedSyntax as exc:
            return self._terminal(
                Verdict.UNANALYZABLE.value, "UNSUPPORTED_SYNTAX", exc,
                rid, digest, sql, ctx,
            )
        except ParseError as exc:
            return self._terminal(
                Verdict.UNANALYZABLE.value, "PARSE_ERROR", exc,
                rid, digest, sql, ctx,
            )
        except LexError as exc:
            return self._terminal(
                Verdict.UNANALYZABLE.value, "LEX_ERROR", exc,
                rid, digest, sql, ctx,
            )
        except SchemaUnavailable as exc:
            return self._terminal(
                Verdict.UNANALYZABLE.value, "SCHEMA_UNAVAILABLE", exc,
                rid, digest, sql, ctx,
            )

        # Map each positional '?' placeholder to its 1-based source ordinal,
        # so list bindings line up with the right token.
        positional = sorted(
            (n for n in _walk_nodes(script) if isinstance(n, ast.Param)
             and n.style == "?"),
            key=lambda n: n.span.start,
        )
        ctx.positional_index = {
            n.span.start: i + 1 for i, n in enumerate(positional)
        }

        # inert occurrences: placeholder-looking text the lexer saw inside
        # strings/comments. Reported as info evidence, never treated as params.
        inert = self._collect_inert(sql)

        if len(script.statements) != 1:
            # Defensive: parser already rejects stacked statements, but keep
            # the rule explicit at this layer too.
            findings.append(Finding(
                "STACKED_STATEMENTS", Severity.ERROR.value,
                "exactly one statement per review is permitted",
            ))

        for stmt in script.statements:
            self._check_statement(stmt, ctx)

        self._check_bindings(ctx)

        if findings:
            verdict = Verdict.REJECT.value
        elif ctx.unanalyzable:
            verdict = Verdict.UNANALYZABLE.value

        result = ReviewResult(
            verdict=verdict,
            request_id=rid,
            sql_digest=digest,
            findings=findings + warnings,
            bound_parameters=ctx.param_evidence,
            identifier_bindings=ctx.ident_evidence,
            inert_occurrences=inert,
            statements=[_stmt_kind(s) for s in script.statements],
            basis={
                "policy_id": self.policy.policy_id,
                "dialect": self.policy.dialect,
                "schema_path": self.schema.path,
                "schema_digest": self.schema.digest,
                "tables": sorted(self.schema.tables),
            },
            limitations=limitations,
            diagnostics={
                "request_id": rid,
                "sql_digest": digest,
                "parameter_refs": sorted(ctx.param_refs, key=str),
                "slot_refs": sorted(ctx.slot_refs),
                "decision": verdict,
                "reason_codes": [f.code for f in findings + warnings],
            },
        )
        return result

    def _terminal(self, verdict, code, exc, rid, digest, sql, ctx):
        span = getattr(exc, "span", None)
        finding = Finding(
            code,
            Severity.ERROR.value,
            str(getattr(exc, "message", exc)),
            span=span.as_dict() if span else None,
        )
        return ReviewResult(
            verdict=verdict,
            request_id=rid,
            sql_digest=digest,
            findings=[finding],
            bound_parameters=ctx.param_evidence,
            identifier_bindings=ctx.ident_evidence,
            inert_occurrences=self._collect_inert(sql),
            statements=[],
            basis={
                "policy_id": self.policy.policy_id,
                "schema_digest": self.schema.digest,
            },
            limitations=list(ctx.limitations),
            diagnostics={
                "request_id": rid,
                "sql_digest": digest,
                "decision": verdict,
                "reason_codes": [code],
                "parser_state": "halted",
                "at_offset": span.start if span else None,
            },
        )

    # ------------------------------------------------------------------ #
    # statements
    # ------------------------------------------------------------------ #

    def _check_statement(self, stmt, ctx: "_Context") -> None:
        kind = _stmt_kind(stmt)
        if kind not in self.policy.allowed_statements:
            ctx.error(
                "STATEMENT_NOT_ALLOWED",
                f"{kind.upper()} is not permitted by policy "
                f"{self.policy.policy_id}",
            )
        if isinstance(stmt, ast.Select):
            self._check_select(stmt, ctx)
        elif isinstance(stmt, ast.Insert):
            self._check_insert(stmt, ctx)
        elif isinstance(stmt, ast.Update):
            self._check_update(stmt, kind, ctx)
        elif isinstance(stmt, ast.Delete):
            self._check_delete(stmt, kind, ctx)

    def _register_table(self, ref: ast.TableRef, kind: str,
                        ctx: "_Context") -> str | None:
        if ref.param is not None:
            # Even when bound to a whitelisted-looking string, a value
            # placeholder can never name a table: that is precisely the
            # table-position injection this review exists to stop.
            key, value, _present = ctx.resolve(ref.param)
            ctx.param_refs.add((key, ref.param.style))
            ctx.param_evidence.append({
                "ref": ref.param.ref, "style": ref.param.style,
                "position": "table(rejected)",
                **redact_value(value, name=key),
            })
            ctx.error(
                "VALUE_USED_AS_IDENTIFIER",
                f"value parameter {ref.param.ref!r} appears in a table-name "
                "position; bind identifiers through ${...} slots",
                span=ref.param.span.as_dict(),
            )
            return None
        if ref.slot is not None:
            self._bind_table_slot(ref.slot, ctx)
            # If the slot resolved to a whitelisted table, expose its alias
            # so qualified columns in the body can resolve.
            resolved = ctx.slot_tables.get(ref.slot.name)
            if resolved and ref.alias:
                ctx.alias_to_table[ref.alias] = resolved
            return None
        name = ref.name if ref.quoted else ref.name.lower()
        tp = self.policy.table(name)
        if tp is None or not self.schema.has_table(name):
            ctx.error(
                "TABLE_NOT_WHITELISTED",
                f"table {name!r} is not declared in the whitelist/schema",
                span=ref.span.as_dict(), detail={"table": name},
            )
        elif kind not in tp.allow:
            ctx.error(
                "TABLE_OP_NOT_ALLOWED",
                f"{kind.upper()} is not allowed on table {name!r}",
                span=ref.span.as_dict(), detail={"table": name, "op": kind},
            )
        alias = ref.alias or name.upper()
        ctx.alias_to_table[alias] = name
        # also register an explicit alias when present (map above already
        # covers it), and make the bare table name usable as its own qualifier
        ctx.alias_to_table.setdefault(name.upper(), name)
        ctx.from_tables.add(name)
        return name

    def _check_select(self, stmt: ast.Select, ctx: "_Context") -> None:
        for ref in stmt.from_tables:
            self._register_table(ref, "select", ctx)
        for join_ref, on in stmt.joins:
            self._register_table(join_ref, "select", ctx)
            if on is not None:
                if isinstance(on, list):
                    for c in on:
                        self._check_column_ref(c, ctx)
                else:
                    self._check_expr(on, ctx)
        # If a table position itself was rejected (value param / bad slot),
        # the body cannot resolve against a catalog; reporting every column
        # as unresolved would be noise. Still run binding checks via params.
        table_position_blocked = any(
            (r.param is not None)
            or (r.slot is not None and r.slot.name not in ctx.slot_tables
                and not self.policy.slot(r.slot.name))
            for r in [*stmt.from_tables, *(j[0] for j in stmt.joins)]
        )
        if table_position_blocked and not ctx.from_tables:
            return self._check_select_bindings_only(stmt, ctx)
        for expr, alias in stmt.items:
            self._check_item_expr(expr, alias, ctx)
        if stmt.where is not None:
            self._check_expr(stmt.where, ctx)
        for g in stmt.group_by:
            self._check_expr(g, ctx)
        if stmt.having is not None:
            self._check_expr(stmt.having, ctx)
        for oi in stmt.order_by:
            self._check_order_expr(oi, ctx)
        self._check_limit(stmt.limit, ctx, "LIMIT")
        self._check_limit(stmt.offset, ctx, "OFFSET")

    def _check_select_bindings_only(self, stmt: ast.Select, ctx: "_Context"
                                    ) -> None:
        """When the FROM clause itself is invalid we still type-check every
        value placeholder in the body (so missing/typed bindings are caught),
        but skip catalog resolution that can only produce noisy findings."""
        for expr, _alias in stmt.items:
            self._bind_params_only(expr, ctx)
        if stmt.where is not None:
            self._bind_params_only(stmt.where, ctx)
        for oi in stmt.order_by:
            if isinstance(oi.expr, ast.Param):
                self._check_order_expr(oi, ctx)
            else:
                self._bind_params_only(oi.expr, ctx)
            if oi.direction_slot is not None:
                self._bind_keyword_slot(oi.direction_slot, ctx,
                                        allowed={"ASC", "DESC"})
        self._check_limit(stmt.limit, ctx, "LIMIT")
        self._check_limit(stmt.offset, ctx, "OFFSET")

    def _bind_params_only(self, e, ctx: "_Context") -> None:
        if isinstance(e, ast.Param):
            if e.array_context:
                self._check_array_param(e, ctx)
            elif e.limit_context:
                self._check_limit(e, ctx, "LIMIT")
            else:
                self._check_param_use(e, ctx)
            return
        if isinstance(e, ast.Slot):
            # an undeclared slot is still worth reporting in this mode
            self._lookup_slot(e, ctx)
            return
        for child in _expr_children(e):
            self._bind_params_only(child, ctx)

    def _check_insert(self, stmt: ast.Insert, ctx: "_Context") -> None:
        if stmt.table is not None:
            name = self._register_table(stmt.table, "insert", ctx)
        else:
            name = None
        if stmt.from_select is not None:
            ctx.unanalyzable.append("INSERT ... SELECT")
            self._check_select(stmt.from_select, ctx)
        # validate declared column list
        declared = [c.name if c.quoted else c.name.lower() for c in stmt.columns]
        for col in stmt.columns:
            self._check_table_column(name, col, ctx)
        for row in stmt.rows:
            if stmt.columns and len(row) != len(stmt.columns):
                ctx.error(
                    "PARAMETER_TYPE_INVALID",
                    f"VALUES row has {len(row)} elements but column list has "
                    f"{len(stmt.columns)}",
                )
            for e in row:
                self._check_expr(e, ctx)
        _ = declared

    def _check_update(self, stmt: ast.Update, kind: str, ctx: "_Context") -> None:
        name = self._register_table(stmt.table, kind, ctx)  # type: ignore[arg-type]
        for col, value in stmt.assignments:
            self._check_table_column(name, col, ctx)
            self._check_expr(value, ctx)
        if stmt.where is not None:
            self._check_expr(stmt.where, ctx)

    def _check_delete(self, stmt: ast.Delete, kind: str, ctx: "_Context") -> None:
        self._register_table(stmt.table, kind, ctx)  # type: ignore[arg-type]
        if stmt.where is not None:
            self._check_expr(stmt.where, ctx)
        else:
            ctx.warn(
                "BINDING_UNUSED",
                "DELETE without WHERE affects every row (reviewer note)",
            )

    # ------------------------------------------------------------------ #
    # columns / expressions
    # ------------------------------------------------------------------ #

    def _check_table_column(self, table_name: str | None, col: ast.ColumnRef,
                            ctx: "_Context") -> None:
        cname = col.name if col.quoted else col.name.lower()
        if table_name is None:
            return
        tp = self.policy.table(table_name)
        if tp is not None and cname not in tp.columns:
            ctx.error(
                "COLUMN_NOT_WHITELISTED",
                f"column {cname!r} is not declared for table {table_name!r}",
                span=col.span.as_dict() if col.span else None,
                detail={"table": table_name, "column": cname},
            )
        elif self.schema.has_table(table_name) and not self.schema.has_column(
            table_name, cname
        ):
            ctx.warn(
                "COLUMN_UNRESOLVED",
                f"column {cname!r} missing from fixture schema of "
                f"{table_name!r} (possible schema drift)",
                span=col.span.as_dict() if col.span else None,
            )

    def _check_column_ref(self, col: ast.ColumnRef, ctx: "_Context") -> None:
        if col.star:
            return
        cname = col.name if col.quoted else col.name.lower()
        if col.qualifier:
            table_name = ctx.alias_to_table.get(col.qualifier)
            if table_name is None:
                # maybe the qualifier is itself an unaliased table name
                if self.policy.table(col.qualifier.lower()):
                    table_name = col.qualifier.lower()
                else:
                    ctx.error(
                        "COLUMN_UNRESOLVED",
                        f"qualifier {col.qualifier!r} does not name a FROM "
                        "table or alias",
                        span=col.span.as_dict() if col.span else None,
                    )
                    return
            if not self.policy.known_column(table_name, cname):
                ctx.error(
                    "COLUMN_NOT_WHITELISTED",
                    f"column {cname!r} is not declared for "
                    f"{table_name!r}",
                    span=col.span.as_dict() if col.span else None,
                )
                return
            return
        # unqualified: must exist in at least one FROM table
        candidates = [t for t in ctx.from_tables
                      if self.policy.known_column(t, cname)]
        if not candidates:
            # quoted/unknown identifier in a value-ish expression is rejected;
            # we never treat an unknown bare name as a safe column.
            ctx.error(
                "COLUMN_UNRESOLVED",
                f"unqualified column {cname!r} cannot be resolved against "
                "any FROM table",
                span=col.span.as_dict() if col.span else None,
            )
        elif len(candidates) > 1 and len(ctx.from_tables) > 1:
            ctx.error(
                "AMBIGUOUS_IDENTIFIER",
                f"column {cname!r} is present in several tables "
                f"{sorted(candidates)}; qualify it",
                span=col.span.as_dict() if col.span else None,
            )

    def _check_item_expr(self, expr, alias, ctx: "_Context") -> None:
        if isinstance(expr, ast.ColumnRef) and expr.star:
            return
        self._check_expr(expr, ctx)
        if alias:
            if isinstance(expr, ast.ColumnRef) and not expr.star:
                table = None
                if expr.qualifier:
                    table = ctx.alias_to_table.get(expr.qualifier)
                else:
                    cands = [t for t in ctx.from_tables
                             if self.policy.known_column(t, expr.name.lower())]
                    table = cands[0] if len(cands) == 1 else None
                if table:
                    ctx.alias_to_table[alias] = table
            # expressions aliases are registered as opaque (no column lookup)

    def _check_order_expr(self, oi: ast.OrderItem, ctx: "_Context") -> None:
        # A value placeholder directly in ORDER BY is the canonical dynamic
        # sort-field injection: order keys are identifiers, not data, so they
        # must go through a declared ${slot}. (SQLite would accept a bound
        # integer as a column ordinal, but policy requires explicit slots.)
        if isinstance(oi.expr, ast.Param):
            key, value, _present = ctx.resolve(oi.expr)
            ctx.param_refs.add((key, oi.expr.style))
            ctx.param_evidence.append({
                "ref": oi.expr.ref, "style": oi.expr.style,
                "position": "order_by(rejected)",
                **redact_value(value, name=key),
            })
            ctx.error(
                "VALUE_USED_AS_IDENTIFIER",
                f"value parameter {oi.expr.ref!r} is used as an ORDER BY "
                "sort field; dynamic identifiers require a ${slot}",
                span=oi.expr.span.as_dict(),
            )
            return
        self._check_expr(oi.expr, ctx, order_position=True)
        if oi.direction_slot is not None:
            self._bind_keyword_slot(oi.direction_slot, ctx,
                                    allowed={"ASC", "DESC"})

    def _check_expr(self, e, ctx: "_Context", *, order_position: bool = False
                    ) -> None:
        if e is None:
            return
        if isinstance(e, ast.Param):
            self._check_param_use(e, ctx, order_position=order_position)
        elif isinstance(e, ast.Slot):
            # A ${slot} in a scalar expression position outside ORDER BY:
            # only meaningful as a column identifier; bind it accordingly.
            if order_position:
                self._bind_column_slot(e, ctx)
            else:
                # ${col} = 'x' style: the slot can still be a column name
                self._bind_column_slot(e, ctx)
        elif isinstance(e, ast.ColumnRef):
            self._check_column_ref(e, ctx)
        elif isinstance(e, ast.Literal):
            return
        elif isinstance(e, ast.FuncCall):
            for a in e.args:
                self._check_expr(a, ctx)
        elif isinstance(e, ast.Unary):
            self._check_expr(e.operand, ctx)
        elif isinstance(e, ast.Binary):
            self._check_expr(e.left, ctx)
            self._check_expr(e.right, ctx)
        elif isinstance(e, ast.InList):
            self._check_expr(e.expr, ctx)
            for it in e.items:
                self._check_in_item(it, ctx)
        elif isinstance(e, ast.Between):
            self._check_expr(e.expr, ctx)
            self._check_expr(e.low, ctx)
            self._check_expr(e.high, ctx)
        elif isinstance(e, ast.IsNull):
            self._check_expr(e.expr, ctx)
        elif isinstance(e, ast.Cast):
            self._check_expr(e.expr, ctx)
        elif isinstance(e, ast.CaseExpr):
            if e.subject:
                self._check_expr(e.subject, ctx)
            for c, r in e.whens:
                self._check_expr(c, ctx)
                self._check_expr(r, ctx)
            if e.default:
                self._check_expr(e.default, ctx)
        else:  # pragma: no cover - exhaustiveness
            ctx.unanalyzable.append(type(e).__name__)

    def _check_in_item(self, item, ctx: "_Context") -> None:
        if isinstance(item, ast.Param):
            # Parser marks single-placeholder IN lists as array context;
            # tuple elements stay scalar.
            if not item.array_context and item.tuple_context:
                self._check_param_use(item, ctx)
            elif item.array_context:
                self._check_array_param(item, ctx)
            else:
                self._check_param_use(item, ctx)
        else:
            self._check_expr(item, ctx)

    def _check_limit(self, arg, ctx: "_Context", label: str) -> None:
        if arg is None:
            return
        if isinstance(arg, ast.Literal):
            if not isinstance(arg.value, int) or isinstance(arg.value, bool) \
                    or arg.value < 0:
                ctx.error(
                    "LIMIT_VALUE_INVALID",
                    f"{label} literal must be a non-negative integer",
                    span=arg.span.as_dict(),
                )
            return
        if isinstance(arg, ast.Param):
            key, value, present = ctx.resolve(arg)
            ctx.param_refs.add((key, arg.style))
            ev = redact_value(value, name=key)
            ctx.param_evidence.append({"ref": arg.ref, "style": arg.style,
                                       "position": label, **ev})
            if not present:
                ctx.error(
                    "PARAMETER_UNBOUND",
                    f"parameter {arg.ref!r} used for {label} has no binding",
                    span=arg.span.as_dict(),
                )
                return
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                ctx.error(
                    "LIMIT_VALUE_INVALID",
                    f"{label} binding must be a non-negative integer",
                    span=arg.span.as_dict(), detail={"provided": ev},
                )

    # ------------------------------------------------------------------ #
    # value parameters
    # ------------------------------------------------------------------ #

    def _check_param_use(self, p: ast.Param, ctx: "_Context", *,
                         order_position: bool = False) -> None:
        _ = order_position
        key, value, present = ctx.resolve(p)
        ctx.param_refs.add((key, p.style))
        if p.array_context:
            self._check_array_param(p, ctx)
            return
        ev = redact_value(value, name=key)
        ctx.param_evidence.append({"ref": p.ref, "style": p.style,
                                   "position": "value", **ev})
        if not present:
            ctx.error(
                "PARAMETER_UNBOUND",
                f"value parameter {p.ref!r} has no binding",
                span=p.span.as_dict(),
            )
            return
        if not isinstance(value, _SCALAR_TYPES):
            ctx.error(
                "PARAMETER_TYPE_INVALID",
                f"parameter {p.ref!r} expects a scalar value but got "
                f"{type(value).__name__}",
                span=p.span.as_dict(), detail={"provided": ev},
            )

    def _check_array_param(self, p: ast.Param, ctx: "_Context") -> None:
        key, value, present = ctx.resolve(p)
        ctx.param_refs.add((key, p.style))
        ev = redact_value(value, name=key)
        ctx.param_evidence.append({"ref": p.ref, "style": p.style,
                                   "position": "array", **ev})
        if not present:
            ctx.error(
                "PARAMETER_UNBOUND",
                f"array parameter {p.ref!r} has no binding",
                span=p.span.as_dict(),
            )
            return
        if not isinstance(value, (list, tuple)):
            ctx.error(
                "PARAMETER_TYPE_INVALID",
                f"parameter {p.ref!r} is used as an array (IN/ANY) but the "
                f"binding is {type(value).__name__}",
                span=p.span.as_dict(), detail={"provided": ev},
            )
            return
        if len(value) == 0 and not self.policy.allow_empty_array:
            ctx.error(
                "ARRAY_EMPTY",
                f"array parameter {p.ref!r} is empty; an empty IN list is "
                "invalid and disallowed by policy",
                span=p.span.as_dict(),
            )
        if len(value) > self.policy.max_array_length:
            ctx.error(
                "ARRAY_TOO_LONG",
                f"array parameter {p.ref!r} has {len(value)} elements, "
                f"limit is {self.policy.max_array_length}",
                span=p.span.as_dict(),
            )
        for i, element in enumerate(value):
            if not isinstance(element, _SCALAR_TYPES):
                ctx.error(
                    "ARRAY_ELEMENT_INVALID",
                    f"element [{i}] of array parameter {p.ref!r} is "
                    f"{type(element).__name__}, expected scalar",
                    span=p.span.as_dict(),
                )

    # ------------------------------------------------------------------ #
    # identifier slots
    # ------------------------------------------------------------------ #

    def _lookup_slot(self, slot: ast.Slot, ctx: "_Context") -> SlotPolicy | None:
        spec = self.policy.slot(slot.name)
        if spec is None:
            ctx.error(
                "IDENTIFIER_SLOT_NOT_DECLARED",
                f"identifier slot ${{{slot.name}}} is not declared in the "
                "policy whitelist",
                span=slot.span.as_dict(), detail={"slot": slot.name},
            )
        ctx.slot_refs.add(slot.name)
        return spec

    def _bind_table_slot(self, slot: ast.Slot, ctx: "_Context") -> None:
        spec = self._lookup_slot(slot, ctx)
        if spec is None:
            return
        if "table" not in spec.roles:
            ctx.error(
                "SLOT_ROLE_MISMATCH",
                f"slot {slot.name!r} is declared for roles "
                f"{sorted(spec.roles)} but used as a table",
                span=slot.span.as_dict(),
            )
            return
        bound = ctx.identifiers.get(slot.name)
        if bound is None:
            ctx.error(
                "IDENTIFIER_SLOT_UNBOUND",
                f"table slot ${{{slot.name}}} has no identifier binding",
                span=slot.span.as_dict(),
            )
            return
        raw, ident = self._validate_binding_syntax(bound, slot, ctx)
        if ident is None:
            return
        key = ident.lower()
        if key not in spec.allowed_tables or not self.schema.has_table(key):
            ctx.error(
                "IDENTIFIER_NOT_ALLOWED",
                f"table identifier {raw!r} for slot {slot.name!r} is not on "
                "the declared table whitelist",
                span=slot.span.as_dict(),
                detail={"slot": slot.name, **redact_identifier(raw)},
            )
            return
        ctx.slot_tables[slot.name] = key
        ctx.from_tables.add(key)
        ctx.alias_to_table[key.upper()] = key
        ctx.ident_evidence.append({
            "slot": slot.name, "role": "table", "bound": key,
            "matched_whitelist": True,
        })

    def _bind_column_slot(self, slot: ast.Slot, ctx: "_Context") -> None:
        spec = self._lookup_slot(slot, ctx)
        if spec is None:
            return
        if "column" not in spec.roles:
            ctx.error(
                "SLOT_ROLE_MISMATCH",
                f"slot {slot.name!r} is declared for roles "
                f"{sorted(spec.roles)} but used as a column",
                span=slot.span.as_dict(),
            )
            return
        bound = ctx.identifiers.get(slot.name)
        if bound is None:
            ctx.error(
                "IDENTIFIER_SLOT_UNBOUND",
                f"column slot ${{{slot.name}}} has no identifier binding",
                span=slot.span.as_dict(),
            )
            return
        raw, ident = self._validate_binding_syntax(bound, slot, ctx)
        if ident is None:
            return
        col_key = ident.lower()
        # resolve scope: explicit qualifier (o.${col}) or any FROM table
        scope_tables: list[str] = []
        if slot.qualifier:
            t = ctx.alias_to_table.get(slot.qualifier)
            if t is None and self.policy.table(slot.qualifier.lower()):
                t = slot.qualifier.lower()
            if t is None:
                ctx.error(
                    "COLUMN_UNRESOLVED",
                    f"qualifier {slot.qualifier!r} on ${{{slot.name}}} does "
                    "not name a FROM table",
                    span=slot.span.as_dict(),
                )
                return
            scope_tables = [t]
        else:
            scope_tables = list(ctx.from_tables)
        allowed = spec.allowed_columns
        matched_table = None
        for t in scope_tables:
            table_allowed = allowed.get(t, frozenset())
            if col_key in table_allowed and self.policy.known_column(t, col_key):
                matched_table = t
                break
            if spec.allow_unqualified_columns and self.policy.known_column(
                t, col_key
            ):
                matched_table = t
                break
        if matched_table is None:
            ctx.error(
                "IDENTIFIER_NOT_ALLOWED",
                f"column identifier {raw!r} for slot {slot.name!r} is not "
                "permitted in this query scope",
                span=slot.span.as_dict(),
                detail={"slot": slot.name, "scope": scope_tables,
                        **redact_identifier(raw)},
            )
            return
        ctx.ident_evidence.append({
            "slot": slot.name, "role": "column", "bound": col_key,
            "table": matched_table, "matched_whitelist": True,
        })

    def _bind_keyword_slot(self, slot: ast.Slot, ctx: "_Context", *,
                           allowed: set[str]) -> None:
        spec = self._lookup_slot(slot, ctx)
        if spec is None:
            return
        if "keyword" not in spec.roles:
            ctx.error(
                "SLOT_ROLE_MISMATCH",
                f"slot {slot.name!r} is declared for roles "
                f"{sorted(spec.roles)} but used as a keyword",
                span=slot.span.as_dict(),
            )
            return
        bound = ctx.identifiers.get(slot.name)
        if bound is None:
            ctx.error(
                "IDENTIFIER_SLOT_UNBOUND",
                f"keyword slot ${{{slot.name}}} has no identifier binding",
                span=slot.span.as_dict(),
            )
            return
        raw, ident = self._validate_binding_syntax(
            bound, slot, ctx, allow_keyword=True
        )
        if ident is None:
            return
        upper = ident.upper()
        effective = spec.allowed_keywords or allowed
        if upper not in effective or upper not in allowed:
            ctx.error(
                "IDENTIFIER_NOT_ALLOWED",
                f"keyword {raw!r} for slot {slot.name!r} is not one of "
                f"{sorted(allowed)}",
                span=slot.span.as_dict(),
                detail={"slot": slot.name, **redact_identifier(raw)},
            )
            return
        ctx.ident_evidence.append({
            "slot": slot.name, "role": "keyword", "bound": upper,
            "matched_whitelist": True,
        })

    def _validate_binding_syntax(self, bound: Any, slot: ast.Slot,
                                 ctx: "_Context", *, allow_keyword: bool = False
                                 ) -> tuple[str, str | None]:
        """A bound identifier must lex as a *single bare identifier*.

        Quoted identifiers, whitespace and punctuation are rejected: the
        whitelist comparison is the only thing allowed to determine what the
        identifier is, so the binding itself must not be able to carry more
        than one token (``orders; DROP ...`` must never parse).
        """
        if not isinstance(bound, str):
            ctx.error(
                "PARAMETER_TYPE_INVALID",
                f"identifier binding for ${{{slot.name}}} must be a string, "
                f"got {type(bound).__name__}",
                span=slot.span.as_dict(),
            )
            return str(bound), None
        raw = bound
        stripped = raw.strip()
        if not stripped or stripped != raw:
            ctx.error(
                "IDENTIFIER_NOT_ALLOWED",
                f"identifier binding for ${{{slot.name}}} is empty or has "
                "surrounding whitespace",
                span=slot.span.as_dict(),
                detail=redact_identifier(raw),
            )
            return raw, None
        try:
            lex_result = tokenize(stripped)
        except LexError:
            ctx.error(
                "IDENTIFIER_NOT_ALLOWED",
                f"identifier binding for ${{{slot.name}}} does not lex as a "
                "valid identifier",
                span=slot.span.as_dict(), detail=redact_identifier(raw),
            )
            return raw, None
        significant = [t for t in lex_result.tokens
                       if t.kind.name != "EOF"]
        valid_kinds = ["IDENT"] + (["KEYWORD"] if allow_keyword else [])
        if len(significant) != 1 or significant[0].kind.name not in valid_kinds:
            ctx.error(
                "IDENTIFIER_NOT_ALLOWED",
                f"identifier binding for ${{{slot.name}}} must be a single "
                "unquoted identifier (quoted/multi-token input rejected)",
                span=slot.span.as_dict(), detail=redact_identifier(raw),
            )
            return raw, None
        return raw, stripped

    # ------------------------------------------------------------------ #
    # bindings bookkeeping
    # ------------------------------------------------------------------ #

    def _check_bindings(self, ctx: "_Context") -> None:
        # every declared identifier binding must have been consumed
        for name in ctx.identifiers:
            if name not in ctx.slot_refs:
                ctx.warn(
                    "BINDING_UNUSED",
                    f"identifier binding {name!r} was supplied but no "
                    "${" + name + "} slot appears in the template",
                )
        # value bindings not referenced by the template
        for ref in ctx.bindings:
            if not any(pref == ref for pref, _style in ctx.param_refs):
                ctx.warn(
                    "BINDING_UNUSED",
                    f"parameter binding {ref!r} was supplied but no matching "
                    "placeholder appears in the template",
                )

    def _collect_inert(self, sql: str) -> list[dict]:
        try:
            result = tokenize(sql)
        except LexError:
            return []
        return [
            {"text": occ.text, "container": occ.container,
             "span": occ.span.as_dict()}
            for occ in result.inert_occurrences
        ]


# ---------------------------------------------------------------------- #
# helper context
# ---------------------------------------------------------------------- #

@dataclasses.dataclass
class _Context:
    policy: Policy
    schema: SchemaSnapshot
    bindings: dict
    identifiers: dict
    errors: list[Finding]
    warnings: list[Finding]
    limitations: list[str]
    alias_to_table: dict[str, str | None] = dataclasses.field(default_factory=dict)
    from_tables: set[str] = dataclasses.field(default_factory=set)
    param_refs: set[tuple] = dataclasses.field(default_factory=set)
    slot_refs: set[str] = dataclasses.field(default_factory=set)
    slot_tables: dict[str, str | None] = dataclasses.field(default_factory=dict)
    param_evidence: list[dict] = dataclasses.field(default_factory=list)
    ident_evidence: list[dict] = dataclasses.field(default_factory=list)
    unanalyzable: list[str] = dataclasses.field(default_factory=list)
    positional_index: dict = dataclasses.field(default_factory=dict)

    def resolve(self, p: "ast.Param") -> tuple[str, Any, bool]:
        """Resolve a parameter to (lookup key, bound value, present)."""
        if p.style == "?":
            key = str(self.positional_index.get(p.span.start, ""))
        else:
            key = p.ref
        present = key in self.bindings
        return key, self.bindings.get(key), present

    def error(self, code: str, message: str, *, span=None, detail=None) -> None:
        self.errors.append(Finding(
            code, Severity.ERROR.value, message,
            span=span, detail=detail or {},
        ))

    def warn(self, code: str, message: str, *, span=None, detail=None) -> None:
        self.warnings.append(Finding(
            code, Severity.WARNING.value, message,
            span=span, detail=detail or {},
        ))


def _normalize_bindings(parameters: dict | list | None) -> dict:
    """Normalize all parameter keys to strings.

    Named (``:x``/``@x``) keys keep their name; numbered (``$1``) and
    positional (``?``) keys become the index as a string ("1", "2", ...).
    """
    if parameters is None:
        return {}
    if isinstance(parameters, list):
        return {str(i + 1): v for i, v in enumerate(parameters)}
    return {str(k): v for k, v in parameters.items()}


def _expr_children(e) -> list:
    """Direct expression children of an expression node."""
    if e is None or isinstance(e, (str, int, float, bool)):
        return []
    children: list = []
    if isinstance(e, ast.FuncCall):
        children.extend(e.args)
    elif isinstance(e, ast.Unary):
        children.append(e.operand)
    elif isinstance(e, ast.Binary):
        children.extend([e.left, e.right])
    elif isinstance(e, ast.InList):
        children.append(e.expr)
        children.extend(e.items)
    elif isinstance(e, ast.Between):
        children.extend([e.expr, e.low, e.high])
    elif isinstance(e, ast.IsNull):
        children.append(e.expr)
    elif isinstance(e, ast.Cast):
        children.append(e.expr)
    elif isinstance(e, ast.CaseExpr):
        if e.subject:
            children.append(e.subject)
        for c, r in e.whens:
            children.extend([c, r])
        if e.default:
            children.append(e.default)
    return children


def _stmt_kind(stmt) -> str:
    return getattr(stmt, "kind", type(stmt).__name__.lower())


def _walk_nodes(script: ast.Script) -> Iterable[Any]:
    """Yield every expression AST node reachable from the script."""
    stack: list = list(script.statements)
    seen: set = set()
    while stack:
        node = stack.pop()
        if node is None or isinstance(node, (str, int, float, bool)):
            continue
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        if isinstance(node, (list, tuple, set, frozenset)):
            stack.extend(node)
            continue
        if hasattr(node, "__dataclass_fields__"):
            for f in dataclasses.fields(node):
                stack.append(getattr(node, f.name))
        # dict/other containers do not appear in the AST
