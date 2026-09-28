"""Restricted Parquet type system.

The supported logical surface is deliberately small and explicit:

Physical primitives:
    BOOLEAN, INT32, INT64, FLOAT, DOUBLE, BYTE_ARRAY
        (BYTE_ARRAY used only with logical type STRING / UTF8)

Groups:
    struct  - optional or required; may be empty (zero fields). An empty struct
              carries no physical data, so its presence is reconstructed from
              the outer record count. See ``levels`` for the documented caveat.
    list    - the canonical 3-level Parquet LIST layout
              ``optional group LIST -> repeated group list -> element``.

Anything else (DECIMAL, DATE, TIMESTAMP, UUID, JSON/BSON, FLOAT16, maps,
intervals, 2-level legacy lists, ...) is *explicitly rejected* at schema build
time with ``UnsupportedLogicalTypeError`` -- never silently reinterpreted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

REQUIRED: Literal["required"] = "required"
OPTIONAL: Literal["optional"] = "optional"
REPEATED: Literal["repeated"] = "repeated"
Repetition = Literal["required", "optional", "repeated"]

# Sentinel used by JSON schemas / expected trees to denote a SQL-style NULL.
NULL = None


class PhysicalType(str, Enum):
    BOOLEAN = "BOOLEAN"
    INT32 = "INT32"
    INT64 = "INT64"
    FLOAT = "FLOAT"
    DOUBLE = "DOUBLE"
    BYTE_ARRAY = "BYTE_ARRAY"


# Which logical types each physical type may carry in the restricted surface.
_PRIMITIVE_SPECS: dict[str, tuple[PhysicalType, str | None]] = {
    "boolean": (PhysicalType.BOOLEAN, None),
    "int32": (PhysicalType.INT32, None),
    "int64": (PhysicalType.INT64, None),
    "float": (PhysicalType.FLOAT, None),
    "double": (PhysicalType.DOUBLE, None),
    "string": (PhysicalType.BYTE_ARRAY, "STRING"),
}

# Logical/physical names we know about but deliberately do NOT support.
_EXPLICITLY_UNSUPPORTED = {
    "decimal", "int8", "int16", "uint8", "uint16", "uint32", "uint64",
    "date", "time", "timestamp", "interval", "uuid", "json", "bson",
    "float16", "enum", "map", "null",
}


class SchemaError(ValueError):
    """Malformed schema description."""


class UnsupportedLogicalTypeError(SchemaError):
    """A logical/physical type outside the supported restricted surface."""


@dataclass(frozen=True)
class PrimitiveNode:
    name: str
    repetition: Repetition
    physical: PhysicalType
    logical: str | None = None  # currently only "STRING"

    def walk(self, dl: int, rl: int, _stack: list[Any]) -> "_SchemaPath":
        raise NotImplementedError  # handled via TreeWalker


@dataclass(frozen=True)
class StructNode:
    name: str
    repetition: Repetition
    fields: tuple["SchemaNode", ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return len(self.fields) == 0


@dataclass(frozen=True)
class ListNode:
    """Canonical Parquet LIST group: ``optional/required group list_name { repeated group list { <element> } }``."""
    name: str
    repetition: Repetition
    element: "SchemaNode"


@dataclass(frozen=True)
class RootNode:
    """Message root, one child per top-level column."""
    name: str
    fields: tuple["SchemaNode", ...] = field(default_factory=tuple)


SchemaNode = PrimitiveNode | StructNode | ListNode
AnyNode = RootNode | SchemaNode


# --------------------------------------------------------------------------- #
# JSON schema parsing
# --------------------------------------------------------------------------- #

def build_schema(description: dict[str, Any]) -> RootNode:
    """Build a RootNode from a JSON-serialisable schema description.

    Shape::

        {
          "name": "message",
          "fields": [ {"name": "id", "type": "int64", "repetition": "required"},
                      {"name": "tags", "type": "list", "repetition": "optional",
                       "element": {"type": "string"}},
                      {"name": "s", "type": "struct", "repetition": "optional",
                       "fields": [{"name": "x", "type": "int32"}]} ]
        }
    """
    if not isinstance(description, dict) or "fields" not in description:
        raise SchemaError("schema must be an object with a 'fields' list")
    name = description.get("name", "schema")
    if not isinstance(name, str):
        raise SchemaError("schema name must be a string")
    fields = tuple(_parse_field(f, top_level=True) for f in description["fields"])
    return RootNode(name=name, fields=fields)


def _repetition_of(desc: dict[str, Any], default: Repetition, top_level: bool) -> Repetition:
    rep = desc.get("repetition", default)
    if rep not in ("required", "optional", "repeated"):
        raise SchemaError(f"invalid repetition {rep!r} for field {desc.get('name')!r}")
    if rep == "repeated" and top_level:
        raise SchemaError("top-level fields cannot be 'repeated'; wrap them in a list")
    return rep  # type: ignore[return-value]


def _parse_field(desc: dict[str, Any], top_level: bool = False,
                 inside_list_element: bool = False) -> SchemaNode:
    if not isinstance(desc, dict) or "name" in desc and not isinstance(desc["name"], str):
        raise SchemaError(f"invalid field description: {desc!r}")
    if "name" not in desc:
        raise SchemaError(f"field missing name: {desc!r}")
    name = desc["name"]
    ftype = desc.get("type")
    if not isinstance(ftype, str):
        raise SchemaError(f"field {name!r} requires a string 'type'")
    ftype_l = ftype.lower()

    if ftype_l == "struct":
        rep = _repetition_of(desc, OPTIONAL, top_level)
        sub = desc.get("fields", [])
        if not isinstance(sub, list):
            raise SchemaError(f"struct {name!r}: 'fields' must be a list")
        children = tuple(_parse_field(f) for f in sub)
        names = [c.name for c in children]
        if len(names) != len(set(names)):
            raise SchemaError(f"struct {name!r}: duplicate child field names")
        return StructNode(name=name, repetition=rep, fields=children)

    if ftype_l == "list":
        rep = _repetition_of(desc, OPTIONAL, top_level)
        if "element" not in desc:
            raise SchemaError(f"list {name!r} requires an 'element' description")
        element = _parse_list_element(desc["element"])
        return ListNode(name=name, repetition=rep, element=element)

    if ftype_l in _PRIMITIVE_SPECS:
        # Elements inside ``repeated group list`` are implicitly optional
        # unless stated; top-level/struct fields default to optional too.
        default_rep = OPTIONAL
        rep = _repetition_of(desc, default_rep, top_level)
        if inside_list_element and rep == "repeated":
            raise SchemaError(f"list element {name!r} must not be declared repeated")
        physical, logical = _PRIMITIVE_SPECS[ftype_l]
        return PrimitiveNode(name=name, repetition=rep, physical=physical, logical=logical)

    if ftype_l in _EXPLICITLY_UNSUPPORTED:
        raise UnsupportedLogicalTypeError(
            f"logical/physical type {ftype!r} on field {name!r} is not supported "
            f"by the restricted validator (supported: "
            f"boolean, int32, int64, float, double, string, struct, list)"
        )
    raise UnsupportedLogicalTypeError(
        f"unknown/unsupported type {ftype!r} on field {name!r}"
    )


def _parse_list_element(desc: dict[str, Any]) -> SchemaNode:
    """Parse the element of a LIST group.

    The canonical element lives under ``repeated group list`` and may itself be a
    struct (named ``element``) or a primitive. Nested lists are expressed with an
    element of type ``list`` (multi-level lists).
    """
    if not isinstance(desc, dict):
        raise SchemaError("list element must be an object")
    ftype = desc.get("type")
    if ftype == "list":
        if "name" not in desc:
            desc = {**desc, "name": "list"}
        # nested list: outer is optional by definition
        if desc.get("repetition", OPTIONAL) not in (OPTIONAL, OPTIONAL):
            raise SchemaError("nested list element group must be optional")
        return _parse_field(desc, inside_list_element=True)
    if ftype == "struct":
        if "name" not in desc:
            desc = {**desc, "name": "element"}
        if desc.get("repetition") not in (None, OPTIONAL):
            raise SchemaError("list struct element must be optional")
        desc = {**desc, "repetition": OPTIONAL}
        return _parse_field(desc, inside_list_element=True)
    # primitive element -> implicit name "element", always optional
    if "name" not in desc:
        desc = {**desc, "name": "element"}
    desc = {**desc, "repetition": OPTIONAL}
    return _parse_field(desc, inside_list_element=True)


def leaf_columns(root: RootNode) -> list["LeafColumn"]:
    """Enumerate physical leaf columns with precomputed maximum levels.

    Empty structs contribute no leaves; they are reconstructed from record
    boundaries (documented limitation surfaced as a warning during validation).
    """
    cols: list[LeafColumn] = []

    def visit(node: AnyNode, path: tuple[str, ...], max_dl: int, max_rl: int,
              rep_increments: tuple[int, ...]) -> None:
        if isinstance(node, RootNode):
            for child in node.fields:
                visit(child, (child.name,), 0, 0, ())
            return
        if isinstance(node, PrimitiveNode):
            leaf_dl = max_dl + (1 if node.repetition == OPTIONAL else 0)
            cols.append(LeafColumn(tuple(path), node, leaf_dl, max_rl,
                                   tuple(rep_increments)))
            return
        if isinstance(node, StructNode):
            if node.is_empty:
                return  # no physical column; presence derived from record count
            child_dl = max_dl + (1 if node.repetition == OPTIONAL else 0)
            for child in node.fields:
                visit(child, path + (child.name,), child_dl, max_rl, rep_increments)
            return
        if isinstance(node, ListNode):
            # outer LIST group: optional adds one DL step
            outer_dl = max_dl + (1 if node.repetition == OPTIONAL else 0)
            # repeated "list" group: +1 DL step, +1 RL step
            list_dl = outer_dl + 1
            list_rl = max_rl + 1
            visit(node.element, path + ("list", node.element.name),
                  list_dl, list_rl, rep_increments + (list_rl,))
            return

    visit(root, (), 0, 0, ())
    return cols


@dataclass(frozen=True)
class LeafColumn:
    path: tuple[str, ...]
    node: PrimitiveNode
    max_definition_level: int
    max_repetition_level: int
    # Repetition-level values for each enclosing repeated group, innermost last.
    rep_increments: tuple[int, ...]
