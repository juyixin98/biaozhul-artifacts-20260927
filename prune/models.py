"""Predicate model: a small AND/OR tree of column predicates.

Wire form (JSON) example::

    {"op": "AND", "children": [
        {"op": "GE", "column": "ts", "value": "2024-03-05T00:00:00+08:00"},
        {"op": "LT", "column": "ts", "value": "2024-03-06T00:00:00+08:00"},
        {"op": "IS_NULL", "column": "name", "negated": false}
    ]}

Leaf ops: EQ, NE, GT, GE, LT, LE, BETWEEN, IN, IS_NULL.
``IS_NULL`` with ``"negated": true`` means IS NOT NULL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence


class Op(str, Enum):
    EQ = "EQ"
    NE = "NE"
    GT = "GT"
    GE = "GE"
    LT = "LT"
    LE = "LE"
    BETWEEN = "BETWEEN"
    IN = "IN"
    IS_NULL = "IS_NULL"
    AND = "AND"
    OR = "OR"


LEAF_OPS = {
    Op.EQ, Op.NE, Op.GT, Op.GE, Op.LT, Op.LE, Op.BETWEEN, Op.IN, Op.IS_NULL,
}
BINARY_OPS = {Op.EQ, Op.NE, Op.GT, Op.GE, Op.LT, Op.LE}
RANGE_OPS = {Op.GT, Op.GE, Op.LT, Op.LE}


@dataclass(frozen=True)
class Leaf:
    op: Op
    column: str
    value: Any = None          # EQ/NE/GT/...: scalar; IN: tuple; BETWEEN: (lo, hi)
    negated: bool = False      # IS_NULL negated => IS NOT NULL

    def requires_null(self) -> Optional[bool]:
        """True if the leaf can only match NULLs, False if only non-NULLs."""
        if self.op is Op.IS_NULL:
            return not self.negated
        return False  # every other comparison rejects NULL (SQL semantics)


@dataclass(frozen=True)
class Node:
    op: Op                    # AND or OR
    children: Sequence["Predicate"] = field(default_factory=tuple)


Predicate = Leaf | Node


class PredicateError(ValueError):
    """Malformed predicate wire form."""


def parse_predicate(obj: Any) -> Predicate:
    if not isinstance(obj, dict) or "op" not in obj:
        raise PredicateError("predicate node must be an object with 'op'")
    try:
        op = Op(obj["op"])
    except ValueError:
        raise PredicateError(f"unknown op {obj['op']!r}")

    if op in (Op.AND, Op.OR):
        raw = obj.get("children")
        if not isinstance(raw, list) or not raw:
            raise PredicateError(f"{op.value} requires a non-empty 'children' list")
        return Node(op=op, children=tuple(parse_predicate(c) for c in raw))

    col = obj.get("column")
    if not isinstance(col, str) or not col:
        raise PredicateError(f"{op.value} requires a string 'column'")

    if op is Op.IS_NULL:
        return Leaf(op=op, column=col, negated=bool(obj.get("negated", False)))

    if op in BINARY_OPS:
        if "value" not in obj:
            raise PredicateError(f"{op.value} requires 'value'")
        return Leaf(op=op, column=col, value=obj["value"])

    if op is Op.BETWEEN:
        bounds = obj.get("value")
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise PredicateError("BETWEEN requires value=[low, high]")
        return Leaf(op=op, column=col, value=tuple(bounds))

    if op is Op.IN:
        vals = obj.get("value")
        if not isinstance(vals, list) or not vals:
            raise PredicateError("IN requires a non-empty value list")
        return Leaf(op=op, column=col, value=tuple(vals))

    raise PredicateError(f"unhandled op {op.value!r}")  # pragma: no cover


def walk(p: Predicate):
    """Yield every leaf in the tree."""
    if isinstance(p, Leaf):
        yield p
    else:
        for c in p.children:
            yield from walk(c)
