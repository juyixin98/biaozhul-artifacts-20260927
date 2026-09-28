"""Canonical schema model + admission control.

A schema is declared as JSON-shaped dictionaries::

    {"name": "root", "type": "struct", "children": [
        {"name": "ids", "type": "list", "nullable": false, "item":
            {"type": "list", "item": {"type": "int32"}}},
        {"name": "s", "type": "struct", "children": [
            {"name": "v", "type": "string"}]},
    ]}

Only the canonical Parquet 3-level ``LIST`` layout is admitted
(``list`` group -> repeated ``element`` group -> item leaf). Legacy 2-level
``repeated`` fields, ``MAP`` and the other unsupported logical types are
rejected explicitly with ``UNSUPPORTED_LOGICAL_TYPE`` / ``LEGACY_LIST_LAYOUT``
rather than silently misinterpreted.

Per-node maximum definition/repetition levels follow the Parquet specification:

* every OPTIONAL or REPEATED node on the path contributes one definition level;
* every REPEATED node contributes one repetition level.

For the canonical list chain the outer ``list`` group is OPTIONAL and the
``element`` group is REPEATED, so a nullable ``list<int32>`` leaf has
max_def == 3 (null list=0, empty list=1, null element=2, value=3) and
max_rep == 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import ErrorCode, error

# Primitive DSL type -> (Parquet physical type, logical annotation).
# int32/int64/boolean/double/binary/string is the deliberately small surface
# required by the contract. DECIMAL, DATE, TIMESTAMP, UUID, JSON, BSON, ENUM,
# FLOAT16, INTERVAL and MAP are intentionally absent (see reject list).
PRIMITIVES: dict[str, tuple[str, Optional[str]]] = {
    "int32": ("INT32", "INT_32"),
    "int64": ("INT64", "INT_64"),
    "boolean": ("BOOLEAN", None),
    "double": ("DOUBLE", None),
    "string": ("BYTE_ARRAY", "STRING"),
    "binary": ("BYTE_ARRAY", None),
}

# Logical types we explicitly refuse, with the reason surfaced to the caller.
REJECTED_LOGICAL_TYPES: dict[str, str] = {
    "map": "MAP is out of the supported surface (struct/list/primitives only)",
    "decimal": "DECIMAL logical type is not supported by this verifier",
    "date": "DATE logical type is not supported by this verifier",
    "timestamp": "TIMESTAMP logical type is not supported by this verifier",
    "uuid": "UUID logical type is not supported by this verifier",
    "json": "JSON logical type is not supported by this verifier",
    "float": "FLOAT (32-bit) is intentionally disabled; use double",
    "float16": "FLOAT16 logical type is not supported",
    "enum": "ENUM logical type is not supported",
}

_MAX_DEPTH = 64


@dataclass
class Node:
    name: str
    kind: str  # "struct" | "list" | "primitive"
    nullable: bool = True
    primitive: Optional[str] = None        # DSL primitive name
    physical: Optional[str] = None         # Parquet physical type
    logical: Optional[str] = None          # Parquet logical annotation
    children: list["Node"] = field(default_factory=list)  # struct
    item: Optional["Node"] = None          # list element item
    # True when this node is the item of a LIST: its null presence is carried
    # by the REPEATED element group, not by its own OPTIONAL flag (Parquet
    # marks the element group optional; the item node itself behaves as
    # required for definition-level purposes).
    is_list_item: bool = False
    # Filled in by Schema._annotate_levels():
    max_def: int = 0
    max_rep: int = 0
    # Repetition level at which *this* list's direct elements repeat. For a
    # list nested under ancestors this is absolute, e.g. for the inner list of
    # list<list<int>> elements repeat at R=2; for the outer list at R=1.
    element_rep: int = 0
    # Cutoff definition level: below this D the node itself is absent.
    # struct: struct-optional contribution; list: outer-list optional
    # contribution; primitive: leaf-optional contribution.
    def_cutoff: int = 0
    path: tuple[str, ...] = ()

    @property
    def is_leaf(self) -> bool:
        return self.kind == "primitive"

    def node_at(self, path: tuple[str, ...]) -> "Node":
        if not path:
            return self
        head, *rest = path
        if self.kind == "struct":
            for c in self.children:
                if c.name == head:
                    return c.node_at(tuple(rest))
        if self.kind == "list":
            if head in ("element", "list"):
                return self.item.node_at(tuple(rest)) if rest else self
        raise KeyError(path)


def _require_str(d: Any, key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise error(
            ErrorCode.INVALID_SCHEMA, f"field '{key}' must be a non-empty string",
            schema_path=where,
        )
    return v


def _build_node(spec: dict[str, Any], depth: int) -> Node:
    if not isinstance(spec, dict):
        raise error(ErrorCode.INVALID_SCHEMA, "schema node must be an object", got=repr(spec))
    if depth > _MAX_DEPTH:
        raise error(ErrorCode.INVALID_SCHEMA, f"schema nesting exceeds {_MAX_DEPTH} levels")
    typ = _require_str(spec, "type", "<root>" if depth == 0 else spec.get("name", "?"))
    nullable = bool(spec.get("nullable", True))

    if typ in REJECTED_LOGICAL_TYPES:
        raise error(
            ErrorCode.UNSUPPORTED_LOGICAL_TYPE,
            REJECTED_LOGICAL_TYPES[typ],
            logical_type=typ,
            name=spec.get("name"),
        )

    if typ == "struct":
        if depth > 0:
            name = _require_str(spec, "name", "struct")
        else:
            name = spec.get("name", "root")
        raw_children = spec.get("children", [])
        if not isinstance(raw_children, list) or not raw_children:
            # Parquet (and PyArrow) cannot represent a zero-field struct.
            raise error(
                ErrorCode.EMPTY_STRUCT,
                "struct with no child fields is not representable in Parquet "
                "(PyArrow refuses 'Cannot write struct type with no child field')",
                struct_name=name,
            )
        children = [_build_node(c, depth + 1) for c in raw_children]
        names = [c.name for c in children]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise error(
                ErrorCode.INVALID_SCHEMA, "duplicate struct field names",
                struct_name=name, duplicates=sorted(dupes),
            )
        return Node(name=name, kind="struct", nullable=nullable, children=children)

    if typ == "list":
        name = _require_str(spec, "name", "list")
        item_spec = spec.get("item")
        if item_spec is None:
            raise error(
                ErrorCode.INVALID_SCHEMA, "list node requires an 'item' node",
                list_name=name,
            )
        # The item field of the synthetic element group must itself carry a
        # name. Anonymous items get the standard name "element".
        if isinstance(item_spec, dict) and "name" not in item_spec:
            item_spec = {**item_spec, "name": "element"}
        # Explicit guard against the legacy 2-level encoding
        # (repeated primitive with no enclosing list group).
        if spec.get("layout") not in (None, "3level"):
            if spec.get("layout") == "2level":
                raise error(
                    ErrorCode.LEGACY_LIST_LAYOUT,
                    "legacy 2-level repeated encoding is not supported; "
                    "use the canonical LIST layout",
                    list_name=name,
                )
            raise error(
                ErrorCode.UNSUPPORTED_LOGICAL_TYPE,
                f"unknown list layout {spec.get('layout')!r}",
                list_name=name,
            )
        item = _build_node(item_spec, depth + 1)
        item.is_list_item = True
        return Node(name=name, kind="list", nullable=True, item=item)

    if typ in PRIMITIVES:
        name = _require_str(spec, "name", typ)
        physical, logical = PRIMITIVES[typ]
        return Node(
            name=name, kind="primitive", nullable=nullable,
            primitive=typ, physical=physical, logical=logical,
        )

    raise error(
        ErrorCode.UNSUPPORTED_LOGICAL_TYPE,
        f"unknown or unsupported type {typ!r}",
        type=typ, name=spec.get("name"),
        supported=sorted(list(PRIMITIVES) + ["struct", "list"]),
    )


class Schema:
    """Validated canonical schema with per-node level metadata."""

    def __init__(self, spec: dict[str, Any], max_nodes: int = 512):
        self.raw = spec
        self.root = _build_node(spec, 0)
        # The implicit root message is always REQUIRED: there is no outer
        # OPTIONAL group contributing a definition level.
        self.root.nullable = False
        self.leaves: list[Node] = []
        self.all_nodes: list[Node] = []
        self._annotate(self.root, parent_def=0, parent_rep=0, path=())
        if len(self.all_nodes) > max_nodes:
            raise error(
                ErrorCode.INVALID_SCHEMA,
                f"schema has {len(self.all_nodes)} nodes, limit is {max_nodes}",
            )

    def _annotate(self, node: Node, parent_def: int, parent_rep: int,
                  path: tuple[str, ...]) -> None:
        node.path = path + (node.name,)
        self.all_nodes.append(node)

        if node.kind == "struct":
            own_def = parent_def + (1 if node.nullable else 0)
            node.def_cutoff = own_def
            node.max_def = own_def
            node.max_rep = parent_rep
            node.element_rep = parent_rep
            for c in node.children:
                self._annotate(c, own_def, parent_rep, node.path)
        elif node.kind == "list":
            # Canonical 3-level LIST: outer group is OPTIONAL (+1 def),
            # element group is REPEATED (+1 def, +1 rep). The physical path
            # includes the synthetic "<name>.list" group so the leaf path
            # matches PyArrow/fastparquet ("a.list.element").
            outer_def = parent_def + 1
            element_def = outer_def + 1
            element_rep = parent_rep + 1
            node.def_cutoff = outer_def
            node.max_def = element_def
            node.max_rep = element_rep
            node.element_rep = element_rep
            assert node.item is not None
            self._annotate(node.item, element_def, element_rep,
                           node.path + ("list",))
        else:
            leaf_def = parent_def + (1 if node.nullable else 0)
            node.def_cutoff = leaf_def
            node.max_def = leaf_def
            node.max_rep = parent_rep
            node.element_rep = parent_rep
            self.leaves.append(node)

    # ------------------------------------------------------------------
    def describe_for_headers(self) -> list[dict[str, Any]]:
        return [
            {
                "path": ".".join(n.path[1:]),  # drop synthetic root
                "max_definition_level": n.max_def,
                "max_repetition_level": n.max_rep,
                "physical": n.physical,
                "logical": n.logical,
                "nullable": n.nullable,
            }
            for n in self.leaves
        ]
