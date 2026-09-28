"""Security kernel: bind-parameter and dynamic-identifier review.

Pipeline
--------
1. tokenize + structurally parse the template (no execution);
2. validate the *form*: statement type, target writability, WHERE guard;
3. validate every value parameter binding — presence, type, array/IN-list
   consistency, parameter allow-list membership;
4. validate every ``{{ slot }}`` — declared policy, whitelist membership,
   fixture schema membership;
5. validate static tables / columns against the read-only fixture catalog;
6. render the template with slots substituted as *quoted identifiers* and all
   values left as ``?`` placeholders, then ask SQLite for an
   ``EXPLAIN QUERY PLAN`` on the read-only fixture. This exercises the planner
   on the exact rendered shape **without executing the statement**.

The kernel returns a structured verdict plus findings and a coverage report;
it never raises on malformed input.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .lexer import TokenKind, tokenize
from .parser import Parser
from .models import (
    Coverage,
    Finding,
    Severity,
    Span,
    Statement,
    Verdict,
)
from .policy import Policy, SlotPolicy
from .redaction import redact_value, value_type_name
from ..state.fixture import ReadOnlyFixture

# extra catalogue entries produced by the kernel itself
_EXTRA_CATALOGUE = {
    "RENDERED_SQL_INVALID": (
        Severity.REJECT,
        "Rendered parameterized SQL is rejected by the SQLite planner",
    ),
}


@dataclass
class ReviewResult:
    verdict: Verdict
    findings: list[Finding] = field(default_factory=list)
    statement_type: str | None = None
    rendered_sql: str | None = None
    resolved_identifiers: dict[str, str] = field(default_factory=dict)
    param_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    advisory_codes: list[str] = field(default_factory=list)
    reject_codes: list[str] = field(default_factory=list)
    unanalyzable_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "statement_type": self.statement_type,
            "rendered_sql": self.rendered_sql,
            "resolved_identifiers": self.resolved_identifiers,
            "param_diagnostics": self.param_diagnostics,
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity.value,
                    "message": f.message,
                    "context": f.context,
                    "location": (
                        {"offset": f.span.start, "line": f.span.line + 1,
                         "col": f.span.col + 1}
                        if f.span else None),
                    "detail": f.detail,
                }
                for f in self.findings
            ],
            "coverage": self.coverage.as_dict(),
            "codes": {
                "reject": self.reject_codes,
                "unanalyzable": self.unanalyzable_codes,
                "advisory": self.advisory_codes,
            },
        }


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _binding_key(marker: str, positional_index: int) -> int | str:
    if marker == "?":
        return positional_index
    if marker.startswith("?") and marker[1:].isdigit():
        return int(marker[1:]) - 1
    return marker[1:]  # strip : @ $


def _lookup(bindings: dict[str, Any], key: int | str) -> tuple[bool, Any]:
    if key in bindings:
        return True, bindings[key]
    if isinstance(key, int):
        for variant in (str(key),):
            if variant in bindings:
                return True, bindings[variant]
    return False, None


class Kernel:
    def __init__(self, policy: Policy, fixture: ReadOnlyFixture | None = None):
        self.policy = policy
        self.fixture = fixture

    def review(self, template: str, params: dict[str, Any] | None = None,
               slots: dict[str, str] | None = None) -> ReviewResult:
        params = params or {}
        slots = slots or {}
        result = ReviewResult(verdict=Verdict.ACCEPT, coverage=Coverage())
        parser = Parser()
        parsed = parser.parse(template)
        result.findings.extend(parsed.findings)

        if any(f.code == "LEX_ERROR" for f in parsed.findings):
            result.coverage.lexed = False
            return self._finalize(result)
        result.coverage.lexed = True

        if not parsed.statements:
            return self._finalize(result)
        stmt = parsed.statements[0]
        result.statement_type = stmt.stmt_type
        result.coverage.statement_type = stmt.stmt_type

        # form checks ----------------------------------------------------------
        if stmt.stmt_type not in self.policy.allowed_statement_types:
            result.findings.append(Finding(
                "STATEMENT_TYPE_NOT_ALLOWED",
                f"{stmt.stmt_type} is not in the allowed statement types",
                context="statement",
                detail={"allowed": sorted(self.policy.allowed_statement_types)}))

        self._check_target_guards(stmt, result)
        self._check_relations(stmt, slots, result)
        self._check_static_columns(stmt, result)
        self._check_slots(stmt, slots, result)
        self._check_params(stmt, params, result)

        fully_parsed = parsed.statements[0].trailing_tokens == 0 and not any(
            f.code == "PARSE_REGION_UNANALYZABLE" for f in parsed.findings)
        result.coverage.statement_parsed = fully_parsed

        # render + planner verify only when every earlier hard rule passed
        hard = {f.code for f in result.findings if f.severity is Severity.REJECT}
        if not hard:
            self._render_and_plan_check(template, stmt, params, slots, result)

        return self._finalize(result)

    # ------------------------------------------------------------------ form

    def _check_target_guards(self, stmt: Statement, result: ReviewResult) -> None:
        if stmt.target is None:
            return
        tgt = stmt.target
        if stmt.stmt_type in ("UPDATE", "DELETE"):
            if self.policy.require_where_for_update_delete and not stmt.has_where:
                result.findings.append(Finding(
                    "MISSING_WHERE",
                    f"{stmt.stmt_type} without WHERE is refused by policy "
                    "(bulk modification guard)",
                    span=tgt.span, context="target"))
        if stmt.stmt_type in ("INSERT", "UPDATE", "DELETE"):
            name = tgt.name
            writable = {n.casefold() for n in self.policy.writable_tables}
            if name is not None and name.casefold() not in writable:
                result.findings.append(Finding(
                    "TARGET_TABLE_NOT_WRITABLE",
                    f"table {name!r} is not declared writable",
                    span=tgt.span, context="target",
                    detail={"table": name,
                            "writable": sorted(self.policy.writable_tables)}))

    def _check_relations(self, stmt: Statement, slots: dict[str, str],
                         result: ReviewResult) -> None:
        # SQLite resolves table/view names case-insensitively (ASCII).
        known_catalog: set[str] = set()
        if self.fixture is not None:
            known_catalog |= {n.casefold() for n in self.fixture.tables().keys()}
        known_catalog |= {n.casefold() for n in self.policy.known_tables}

        for rel in stmt.relations + ([stmt.target] if stmt.target else []):
            entry: dict[str, Any] = {"kind": rel.kind, "span": rel.span.start}
            if rel.kind == "subquery":
                entry["result"] = "skipped"
                entry["reason"] = "derived table; column resolution inside " \
                                  "subqueries is out of static scope"
                result.coverage.skipped.append(
                    "subquery-derived relation catalog check")
            elif rel.kind == "table_function":
                entry["result"] = "skipped"
                entry["reason"] = "table-valued function"
            elif rel.kind == "cte":
                entry["relation"] = rel.name
                entry["result"] = "accepted"
                entry["reason"] = "WITH-defined CTE name"
            elif rel.slot_name is not None:
                chosen = slots.get(rel.slot_name)
                entry["slot"] = rel.slot_name
                entry["relation"] = chosen
                if chosen and chosen.casefold() in known_catalog:
                    entry["result"] = "accepted"
                elif chosen:
                    entry["result"] = "not_in_catalog"
                else:
                    entry["result"] = "unresolved"
            elif rel.name is not None:
                entry["relation"] = rel.name
                if not self.policy.check_static_tables:
                    entry["result"] = "skipped"
                    entry["reason"] = "schema checks disabled by policy"
                elif rel.name.casefold() in known_catalog:
                    entry["result"] = "accepted"
                else:
                    entry["result"] = "unknown"
                    result.findings.append(Finding(
                        "UNKNOWN_TABLE",
                        f"static table {rel.name!r} is absent from the fixture catalog",
                        span=rel.span, context="relation",
                        detail={"table": rel.name}))
            result.coverage.relation_checks.append(entry)

    def _check_static_columns(self, stmt: Statement, result: ReviewResult) -> None:
        if not self.policy.check_static_columns:
            result.coverage.skipped.append("column checks (disabled by policy)")
            return
        if self.fixture is None:
            result.coverage.skipped.append(
                "column checks (no read-only fixture attached)")
            return
        target_name = stmt.target.name if stmt.target else None
        if not target_name or not self.fixture.has_table(target_name):
            return
        for col in stmt.insert_columns + stmt.set_columns:
            ok = self.fixture.has_column(target_name, col.name)
            result.coverage.column_checks.append({
                "column": col.name, "context": col.context,
                "table": target_name, "result": "accepted" if ok else "unknown",
            })
            if not ok:
                result.findings.append(Finding(
                    "UNKNOWN_TABLE",
                    f"static column {col.name!r} is absent from table "
                    f"{target_name!r} in the fixture catalog",
                    span=col.span, context=col.context,
                    detail={"table": target_name, "column": col.name}))

    # ------------------------------------------------------------------ slots

    def _check_slots(self, stmt: Statement, slots: dict[str, str],
                     result: ReviewResult) -> None:
        known_catalog: set[str] = set()
        if self.fixture is not None:
            known_catalog |= {n.casefold() for n in self.fixture.tables().keys()}
        known_catalog |= {n.casefold() for n in self.policy.known_tables}

        seen: set[str] = set()
        for use in stmt.slots:
            policy = self.policy.slot(use.name)
            if policy is None:
                result.findings.append(Finding(
                    "SLOT_UNDECLARED",
                    f"slot {{{{ {use.name} }}}} is not declared in the policy whitelist",
                    span=use.span, context=use.context, detail={"slot": use.name}))
                continue
            chosen = slots.get(use.name)
            if chosen is None and policy.default is not None:
                chosen = policy.default
            if chosen is None:
                result.findings.append(Finding(
                    "MISSING_IDENTIFIER",
                    f"no identifier supplied for slot {{{{ {use.name} }}}}",
                    span=use.span, context=use.context, detail={"slot": use.name}))
                continue
            if chosen not in policy.allowed_identifiers:
                result.findings.append(Finding(
                    "IDENTIFIER_NOT_WHITELISTED",
                    f"identifier {chosen!r} for slot {use.name!r} is not in the "
                    "declared whitelist",
                    span=use.span, context=use.context,
                    detail={"slot": use.name, "supplied_type": "str",
                            "allowed": sorted(policy.allowed_identifiers)}))
                continue
            if policy.require_in_schema and use.context.endswith("relation") \
                    and chosen.casefold() not in known_catalog \
                    and self.policy.check_static_tables:
                result.findings.append(Finding(
                    "UNKNOWN_TABLE",
                    f"dynamic relation {chosen!r} is absent from the fixture catalog",
                    span=use.span, context=use.context,
                    detail={"slot": use.name, "table": chosen}))
                continue
            if use.name not in seen:
                result.resolved_identifiers[use.name] = chosen
                seen.add(use.name)

    # ----------------------------------------------------------------- params

    def _check_params(self, stmt: Statement, params: dict[str, Any],
                      result: ReviewResult) -> None:
        used_keys: set[int | str] = set()
        positional = 0
        # SQLite forbids mixing anonymous ? with numbered ?NNN markers.
        has_anon = any(p.marker == "?" for p in stmt.params)
        has_numbered = any(
            p.marker.startswith("?") and p.marker[1:].isdigit()
            for p in stmt.params)
        mixed = has_anon and has_numbered
        for use in stmt.params:
            key = _binding_key(use.marker, positional)
            if use.marker == "?":
                positional += 1
            if mixed:
                result.param_diagnostics.append({
                    "marker": use.marker, "occurrence": use.occurrence,
                    "context": use.context, "in_expansion_list": use.expansion,
                    "key": str(key), "decision": "rejected",
                    "reason": "anonymous ? and numbered ?NNN markers are mixed",
                })
                result.findings.append(Finding(
                    "MIXED_PARAMETER_STYLES",
                    f"parameter {use.marker} mixes anonymous and ?NNN styles",
                    span=use.span, context=use.context,
                    detail={"marker": use.marker}))
                continue
            present, value = _lookup(params, key)
            diag: dict[str, Any] = {
                "marker": use.marker,
                "occurrence": use.occurrence,
                "context": use.context,
                "in_expansion_list": use.expansion,
                "key": str(key),
            }
            if not present:
                diag["decision"] = "rejected"
                diag["reason"] = "no binding supplied"
                result.param_diagnostics.append(diag)
                result.findings.append(Finding(
                    "MISSING_BINDING",
                    f"no value supplied for parameter {use.marker} "
                    f"(key {key!r}, {use.context})",
                    span=use.span, context=use.context,
                    detail={"marker": use.marker, "key": str(key)}))
                continue
            used_keys.add(key)
            diag["binding"] = redact_value(value)

            decision, reason, codes = self._classify_value(use, key, value)
            diag["decision"] = decision
            diag["reason"] = reason
            result.param_diagnostics.append(diag)
            for code in codes:
                result.findings.append(Finding(
                    code,
                    {
                        "ARRAY_PARAM_IN_SCALAR_CONTEXT":
                            f"array bound to {use.marker} is only legal inside "
                            "an IN (...) list",
                        "EMPTY_EXPANSION":
                            f"empty array bound to {use.marker} would render IN ()",
                        "INVALID_PARAM_TYPE":
                            f"value for {use.marker} has unsupported type",
                        "PARAM_VALUE_NOT_ALLOWED":
                            f"value for {use.marker} is outside its allow-list",
                    }[code],
                    span=use.span, context=use.context,
                    detail={"marker": use.marker, "key": str(key),
                            **redact_value(value)}))

        # unused supplied bindings (advisory — catches caller mistakes)
        supplied = set(params.keys())
        supplied_norm: set[int | str] = set()
        for k in supplied:
            if k.isdigit():
                supplied_norm.add(int(k))
            else:
                supplied_norm.add(k)
        for extra in sorted(supplied_norm - used_keys, key=str):
            result.findings.append(Finding(
                "UNUSED_BINDING",
                f"supplied binding {extra!r} is not referenced by the template",
                context="bindings", detail={"key": str(extra)}))

    def _classify_value(self, use, key: int | str, value: Any):
        codes: list[str] = []
        policy = self.policy.param(str(key))
        if isinstance(value, (list, tuple)):
            if not use.expansion:
                codes.append("ARRAY_PARAM_IN_SCALAR_CONTEXT")
                return "rejected", "array used in scalar context", codes
            if len(value) == 0:
                codes.append("EMPTY_EXPANSION")
                return "rejected", "empty array for IN-list", codes
            if len(value) > self.policy.max_array_length:
                codes.append("INVALID_PARAM_TYPE")
                return "rejected", "array exceeds max_array_length", codes
            bad_type = next(
                (v for v in value if value_type_name(v) not in policy.scalar_types
                 and value_type_name(v) != "array"), None)
            if bad_type is not None:
                codes.append("INVALID_PARAM_TYPE")
                return "rejected", f"array element type {value_type_name(bad_type)} unsupported", codes
            if policy.allowed_values:
                outside = [v for v in value if v not in policy.allowed_values]
                if outside:
                    codes.append("PARAM_VALUE_NOT_ALLOWED")
                    return "rejected", "one or more array elements outside allow-list", codes
            return "accepted", "array expanded inside IN-list", codes

        tn = value_type_name(value)
        if tn not in policy.scalar_types:
            codes.append("INVALID_PARAM_TYPE")
            return "rejected", f"unsupported scalar type {tn}", codes
        if policy.allowed_values and value not in policy.allowed_values:
            codes.append("PARAM_VALUE_NOT_ALLOWED")
            return "rejected", "value outside allow-list", codes
        if use.expansion:
            return "accepted", "scalar used as single-element IN-list", codes
        return "accepted", "scalar value parameter", codes

    # --------------------------------------------------------- render + plan

    def _render_and_plan_check(self, template: str, stmt: Statement,
                               params: dict[str, Any], slots: dict[str, str],
                               result: ReviewResult) -> None:
        try:
            rendered, n_params = self._render(
                template, stmt, params, slots, result)
        except _RenderUnresolved:
            return  # a prior finding already explains why
        result.rendered_sql = rendered
        if self.fixture is None:
            result.coverage.skipped.append(
                "planner verification (no read-only fixture attached)")
            return
        try:
            # The planner check only proves structural validity (known tables,
            # columns, sane syntax for the exact rendered shape). Bind NULL to
            # every placeholder: plan-level name resolution does not depend on
            # values, and nothing is executed.
            cur = self.fixture.connection.execute(
                "EXPLAIN QUERY PLAN " + rendered, [None] * n_params)
            rows = cur.fetchall()
            cur.close()
            # A successful prepare/execute of EXPLAIN QUERY PLAN is the signal;
            # some DML forms legally yield zero output rows.
        except sqlite3.Error as exc:
            result.findings.append(Finding(
                "RENDERED_SQL_INVALID",
                f"SQLite planner rejected rendered SQL: {exc}",
                context="render", detail={"error_type": type(exc).__name__}))

    def _render(self, template: str, stmt: Statement, params: dict[str, Any],
                slots: dict[str, str], result: ReviewResult) -> str:
        tokens = tokenize(template)
        # map param occurrence -> value (to expand IN-list arrays)
        occ_values: dict[int, Any] = {}
        positional = 0
        for use in stmt.params:
            key = _binding_key(use.marker, positional)
            if use.marker == "?":
                positional += 1
            present, value = _lookup(params, key)
            if present:
                occ_values[use.occurrence] = value
        slot_values: dict[int, tuple[str, SlotPolicy]] = {}
        for use in stmt.slots:
            pol = self.policy.slot(use.name)
            chosen = slots.get(use.name) or (pol.default if pol else None)
            if chosen is None or pol is None:
                raise _RenderUnresolved()
            slot_values[use.occurrence] = (chosen, pol)

        out: list[str] = []
        param_idx = 0
        slot_idx = 0
        n_placeholders = 0
        for tok in tokens:
            if tok.kind is TokenKind.BIND_PARAM:
                val = occ_values.get(param_idx)
                if isinstance(val, (list, tuple)):
                    n = max(len(val), 1)
                    out.append(", ".join("?" for _ in range(n)))
                    n_placeholders += n
                else:
                    out.append("?")
                    n_placeholders += 1
                param_idx += 1
            elif tok.kind is TokenKind.SLOT:
                entry = slot_values.get(slot_idx)
                if entry is None:
                    raise _RenderUnresolved()
                chosen, pol = entry
                if pol.quote:
                    out.append(_quote_identifier(chosen))
                else:
                    # bare keyword slot: whitelist already constrains the value;
                    # enforce the lexical shape again at render time
                    if not re.match(pol.bare_word_pattern, chosen):
                        raise _RenderUnresolved()
                    out.append(chosen)
                slot_idx += 1
            elif tok.kind is TokenKind.STRING:
                out.append("'" + tok.value.replace("'", "''") + "'")
            elif tok.kind is TokenKind.BLOB:
                out.append("x'" + tok.value + "'")
            elif tok.kind is TokenKind.IDENTIFIER:
                out.append(_quote_identifier(tok.value))
            elif tok.kind in (TokenKind.LINE_COMMENT, TokenKind.BLOCK_COMMENT):
                continue
            elif tok.kind is TokenKind.EOF:
                continue
            else:
                out.append(tok.value)
        return " ".join(out), n_placeholders

    # ------------------------------------------------------------- aggregate

    def _finalize(self, result: ReviewResult) -> ReviewResult:
        # register kernel-extra catalogue messages lazily
        from .models import FINDING_CATALOGUE
        for code, (sev, msg) in _EXTRA_CATALOGUE.items():
            FINDING_CATALOGUE.setdefault(code, (sev, msg))

        for f in result.findings:
            if f.severity is Severity.REJECT:
                result.reject_codes.append(f.code)
            elif f.severity is Severity.UNANALYZABLE:
                result.unanalyzable_codes.append(f.code)
            else:
                result.advisory_codes.append(f.code)

        if result.reject_codes:
            result.verdict = Verdict.REJECT
        elif result.unanalyzable_codes:
            result.verdict = Verdict.UNANALYZABLE
        else:
            result.verdict = Verdict.ACCEPT
        return result


class _RenderUnresolved(Exception):
    pass
