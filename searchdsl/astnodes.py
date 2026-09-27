"""Query tree node definitions.

Every node carries an optional ``pos`` (character offset into the source
text) used for error locations. Positions are metadata only: they are
stripped from the canonical form so that two semantically identical trees
have identical canonical JSON and hashes.

Canonical leaf encodings::

    {"op": "match_all"}
    {"op": "match_none"}
    {"op": "term",  "field": "title" | null, "value": "cat"}
    {"op": "phrase","field": "body"  | null, "value": "quick brown"}
    {"op": "range", "field": "year",
     "gte": "2000", "gt": null, "lte": null, "lt": "2010"}

The boundary keys are always ``gte`` / ``gt`` / ``lte`` / ``lt`` with
unused keys set to null, and at least one boundary must be non-null.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Optional

# Sentinel field name for an unfielded clause (searches the default fields).
ALL_FIELDS: Optional[str] = None


@dataclass(frozen=True)
class Pos:
    """Source location of a token or clause, 0-indexed character offsets."""

    start: int
    end: int

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end}


class Node:
    """Marker base class; every concrete node is a frozen dataclass with a
    trailing ``pos: Optional[Pos] = None`` field."""

    pos: Optional[Pos]

    def with_pos(self, pos: Optional[Pos]) -> "Node":
        return replace(self, pos=pos)  # type: ignore[arg-type]

    def to_canonical(self) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class MatchAll(Node):
    """Empty query. Matches every document that has any searchable content."""

    pos: Optional[Pos] = None

    def to_canonical(self) -> dict:
        return {"op": "match_all"}


@dataclass(frozen=True)
class MatchNone(Node):
    """A contradiction produced by simplification (e.g. x AND NOT x)."""

    pos: Optional[Pos] = None

    def to_canonical(self) -> dict:
        return {"op": "match_none"}


@dataclass(frozen=True)
class Term(Node):
    value: str = ""
    field_name: Optional[str] = ALL_FIELDS
    pos: Optional[Pos] = None

    def to_canonical(self) -> dict:
        return {"op": "term", "field": self.field_name, "value": self.value}


@dataclass(frozen=True)
class Phrase(Node):
    value: str = ""
    field_name: Optional[str] = ALL_FIELDS
    pos: Optional[Pos] = None

    def to_canonical(self) -> dict:
        return {"op": "phrase", "field": self.field_name, "value": self.value}


@dataclass(frozen=True)
class Range(Node):
    field_name: str = ""
    gte: Optional[str] = None
    gt: Optional[str] = None
    lte: Optional[str] = None
    lt: Optional[str] = None
    pos: Optional[Pos] = None

    def to_canonical(self) -> dict:
        return {
            "op": "range",
            "field": self.field_name,
            "gte": self.gte,
            "gt": self.gt,
            "lte": self.lte,
            "lt": self.lt,
        }


@dataclass(frozen=True)
class And(Node):
    children: tuple["Node", ...] = ()
    pos: Optional[Pos] = None

    def to_canonical(self) -> dict:
        return {"op": "and", "children": [c.to_canonical() for c in self.children]}


@dataclass(frozen=True)
class Or(Node):
    children: tuple["Node", ...] = ()
    pos: Optional[Pos] = None

    def to_canonical(self) -> dict:
        return {"op": "or", "children": [c.to_canonical() for c in self.children]}


@dataclass(frozen=True)
class Not(Node):
    child: "Node" = MatchAll()
    pos: Optional[Pos] = None

    def to_canonical(self) -> dict:
        return {"op": "not", "child": self.child.to_canonical()}


# Deterministic ranking so canonical child ordering is total.
_OP_RANK = {
    "term": 0,
    "phrase": 1,
    "range": 2,
    "not": 3,
    "and": 4,
    "or": 5,
    "match_none": 6,
    "match_all": 7,
}


def _sort_key(node: Node):
    c = node.to_canonical()
    return (
        _OP_RANK.get(c["op"], 9),
        c.get("field") or "",
        c.get("value", ""),
        canonical_json(node),
    )


def sort_nodes(nodes: tuple[Node, ...]) -> tuple[Node, ...]:
    return tuple(sorted(nodes, key=_sort_key))


def to_canonical_dict(node: Node) -> dict:
    return node.to_canonical()


def canonical_json(node: Node, *, indent: Optional[int] = None) -> str:
    """Stable JSON text for a tree: sorted keys, no whitespace surprises."""
    return json.dumps(
        node.to_canonical(),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":") if indent is None else (",", ": "),
        indent=indent,
    )


def canonical_hash(node: Node) -> str:
    """SHA-256 of the canonical form; identity key for stored queries."""
    return hashlib.sha256(canonical_json(node).encode("utf-8")).hexdigest()


def clone_node(node: Node) -> Node:
    """Deep copy of a tree (positions preserved)."""
    if isinstance(node, And) or isinstance(node, Or):
        children = tuple(clone_node(c) for c in node.children)
        return replace(node, children=children)
    if isinstance(node, Not):
        return replace(node, child=clone_node(node.child))
    return node


def first_pos(*nodes: Optional[Node]) -> Optional[Pos]:
    """Earliest non-null source position among the given nodes."""
    found = [n.pos for n in nodes if n is not None and n.pos is not None]
    if not found:
        return None
    p = min(found, key=lambda x: x.start)
    return Pos(p.start, p.end)
