"""Pre-execution validation: whitelist, types, complexity budget.

This runs on the **parsed** tree before normalization (see
:mod:`searchdsl.normalize`), collecting every problem it can and raising
the first one found in source order. Running here means:

* an unknown field is reported even if simplification could erase the
  clause (e.g. ``nonexistent:x OR y``) — nonexistent-field semantics are
  preserved;
* the depth / clause budget is measured on what the user actually typed
  (plus implicit conjunction), not on the smaller normalized tree, so the
  budget cannot be bypassed with redundancy.
"""

from __future__ import annotations

from dataclasses import dataclass

from searchdsl.analysis import parse_date, parse_int, tokenize
from searchdsl.astnodes import (
    And,
    MatchAll,
    MatchNone,
    Node,
    Not,
    Or,
    Phrase,
    Range,
    Term,
)
from searchdsl.config import Limits
from searchdsl.errors import ErrorLocation, SearchDSLError, ValidationError
from searchdsl.spec import FieldSpec, Schema


@dataclass(frozen=True)
class BudgetReport:
    depth: int
    clauses: int
    query_terms: int
    phrase_terms: int

    def as_dict(self) -> dict:
        return {
            "depth": self.depth,
            "clauses": self.clauses,
            "query_terms": self.query_terms,
            "phrase_terms": self.phrase_terms,
        }


def measure(node: Node) -> BudgetReport:
    """Measure the tree against the budget dimensions."""
    return _measure(node, depth=1)


def _measure(node: Node, depth: int) -> BudgetReport:
    if isinstance(node, (And, Or)):
        if not node.children:
            sub = [BudgetReport(depth, 0, 0, 0)]
        else:
            sub = [_measure(c, depth + 1) for c in node.children]
        return BudgetReport(
            depth=max(s.depth for s in sub),
            clauses=sum(s.clauses for s in sub),
            query_terms=sum(s.query_terms for s in sub),
            phrase_terms=max((s.phrase_terms for s in sub), default=0),
        )
    if isinstance(node, Not):
        return _measure(node.child, depth + 1)
    if isinstance(node, (MatchAll, MatchNone)):
        return BudgetReport(depth, 0, 0, 0)
    if isinstance(node, Phrase):
        toks = tokenize(node.value)
        return BudgetReport(depth, 1, len(toks), len(toks))
    if isinstance(node, Range):
        return BudgetReport(depth, 1, 0, 0)
    if isinstance(node, Term):
        return BudgetReport(depth, 1, len(tokenize(node.value)), 0)
    raise SearchDSLError(f"unknown node type during measurement: {type(node)!r}")


def _err(code: str, message: str, node: Node) -> ValidationError:
    pos = ErrorLocation(node.pos.start, node.pos.end) if node.pos else None
    return ValidationError(message, pos=pos, code=code)


def _check_boundary_values(field: FieldSpec, node: Range):
    """Parse every present bound as the field's type; compare ordering."""

    def convert(raw: str):
        if field.type == "int":
            try:
                v = parse_int(raw)
            except ValueError:
                raise _err("VALUE_MALFORMED", f"{field.name}: {raw!r} is not an integer", node)
            return v
        if field.type == "date":
            try:
                v = parse_date(raw)
            except ValueError:
                raise _err("VALUE_MALFORMED", f"{field.name}: {raw!r} is not an ISO date", node)
            return v
        raise _err(
            "FIELD_TYPE_MISMATCH",
            f"range queries require an int or date field, {field.name!r} is {field.type}",
            node,
        )

    low_v = None
    high_v = None
    low_inclusive = node.gte is not None
    low_raw = node.gte if low_inclusive else node.gt
    high_inclusive = node.lte is not None
    high_raw = node.lte if high_inclusive else node.lt
    if low_raw is not None:
        low_v = convert(low_raw)
    if high_raw is not None:
        high_v = convert(high_raw)
    if low_v is not None and high_v is not None:
        if low_v > high_v or (low_v == high_v and not (low_inclusive and high_inclusive)):
            raise _err(
                "RANGE_EMPTY",
                f"empty range on {field.name}: {low_raw!r} .. {high_raw!r}",
                node,
            )


def _check_leaves(node: Node, schema: Schema):
    if isinstance(node, (And, Or)):
        for c in node.children:
            _check_leaves(c, schema)
        return
    if isinstance(node, Not):
        _check_leaves(node.child, schema)
        return
    if isinstance(node, (MatchAll, MatchNone)):
        return

    field_name = getattr(node, "field_name", None)
    if field_name in (None, ""):
        if isinstance(node, Range):
            # An unfielded range has no target type; always invalid.
            raise _err(
                "FIELD_TYPE_MISMATCH",
                "range queries require a field qualifier (e.g. year:[2000 TO 2020])",
                node,
            )
        # Unfielded term/phrase: validated against default fields at query
        # time (they must all be text — guaranteed by the schema loader).
        if isinstance(node, Phrase) and not schema.default_fields:
            raise _err(
                "FIELD_TYPE_MISMATCH",
                "no text default fields configured for an unfielded phrase",
                node,
            )
        if isinstance(node, Term):
            if not tokenize(node.value):
                raise _err(
                    "VALUE_MALFORMED",
                    f"term {node.value!r} contains no searchable characters",
                    node,
                )
        return

    if not schema.has(field_name):
        raise _err(
            "FIELD_UNKNOWN",
            f"unknown field {field_name!r}; allowed: {', '.join(sorted(schema.fields))}",
            node,
        )
    field = schema.get(field_name)

    if isinstance(node, Phrase):
        if not field.supports_phrase():
            raise _err(
                "FIELD_TYPE_MISMATCH",
                f"phrase search requires a text field; {field_name!r} is {field.type}",
                node,
            )
        if not tokenize(node.value):
            raise _err(
                "VALUE_MALFORMED",
                f"phrase {node.value!r} contains no searchable characters",
                node,
            )
        return

    if isinstance(node, Range):
        _check_boundary_values(field, node)
        return

    if isinstance(node, Term):
        if field.type == "text":
            if not tokenize(node.value):
                raise _err(
                    "VALUE_MALFORMED",
                    f"term {node.value!r} contains no searchable characters",
                    node,
                )
        elif field.type == "keyword":
            if node.value == "":
                raise _err("VALUE_MALFORMED", "keyword term cannot be empty", node)
        elif field.type == "int":
            try:
                parse_int(node.value)
            except ValueError:
                raise _err(
                    "VALUE_MALFORMED",
                    f"{field_name}: {node.value!r} is not an integer",
                    node,
                )
        elif field.type == "date":
            try:
                parse_date(node.value)
            except ValueError:
                raise _err(
                    "VALUE_MALFORMED",
                    f"{field_name}: {node.value!r} is not an ISO date (YYYY-MM-DD)",
                    node,
                )
        return

    raise SearchDSLError(f"unknown node type during validation: {type(node)!r}")


def validate(node: Node, schema: Schema, limits: Limits) -> BudgetReport:
    """Validate the parsed tree. Raises the first violation found."""
    _check_leaves(node, schema)
    report = measure(node)
    if report.depth > limits.max_nesting_depth:
        raise ValidationError(
            f"nesting depth {report.depth} exceeds budget {limits.max_nesting_depth}",
            pos=ErrorLocation(0, 0),
            code="BUDGET_DEPTH",
            detail={"depth": report.depth, "limit": limits.max_nesting_depth},
        )
    if report.clauses > limits.max_clauses:
        raise ValidationError(
            f"clause count {report.clauses} exceeds budget {limits.max_clauses}",
            code="BUDGET_CLAUSES",
            detail={"clauses": report.clauses, "limit": limits.max_clauses},
        )
    if report.query_terms > limits.max_query_terms:
        raise ValidationError(
            f"query term count {report.query_terms} exceeds budget {limits.max_query_terms}",
            code="BUDGET_QUERY_TERMS",
            detail={"query_terms": report.query_terms, "limit": limits.max_query_terms},
        )
    if report.phrase_terms > limits.max_phrase_terms:
        raise ValidationError(
            f"phrase length {report.phrase_terms} exceeds budget {limits.max_phrase_terms}",
            code="BUDGET_PHRASE_TERMS",
            detail={"phrase_terms": report.phrase_terms, "limit": limits.max_phrase_terms},
        )
    return report
