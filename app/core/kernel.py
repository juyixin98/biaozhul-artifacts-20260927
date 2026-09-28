"""Execution kernel: tree values <-> Parquet definition/repetition level slot streams.

This module is the system-under-test core. It never reads the expected answer
from itself: external oracles (hand-written expected trees, PyArrow value
round trips and fastparquet level parsing) live in :mod:`app.adapters` and in
the test fixtures.

Encoding rules implemented (Parquet ``DataPageV1/V2`` semantics):

* an OPTIONAL field contributes one definition level;
* a REPEATED field contributes one definition level AND one repetition level;
* the repetition level of the first slot of a value is *inherited* from the
  enclosing context -- it is 0 only for a top-level record start;
* absent optional values emit exactly one NULL marker slot
  ``(d = inherited_def - 1, r = inherited_r)`` carrying no value.

Those rules give, for a nullable ``list<int32>`` leaf (max_def=3, max_rep=1):

    value list  [1, null, 3] -> D (3,2,3), R (0,1,1)
    empty list  []           -> D (1,),    R (0,)
    NULL list   None         -> D (0,),    R (0,)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import ErrorCode, error
from .schema import Node, Schema

# Sentinel carried by NULL-marker slots. Using None is impossible because
# None is a perfectly legal value for nullable primitive leaves.
NULL = object()


@dataclass
class Slot:
    """One (definition level, repetition level, value) triple on a leaf."""

    d: int
    r: int
    value: Any = NULL

    @property
    def is_null_marker(self) -> bool:
        return self.value is NULL


@dataclass
class LeafColumn:
    node: Node
    slots: list[Slot] = field(default_factory=list)

    def append(self, slot: Slot) -> None:
        self.slots.append(slot)


@dataclass
class EncodedTable:
    schema: Schema
    columns: list[LeafColumn]

    def column_by_path(self, path: str) -> LeafColumn:
        for c in self.columns:
            if ".".join(c.node.path[1:]) == path:
                return c
        raise KeyError(path)

    @property
    def record_count(self) -> int:
        if not self.columns:
            return 0
        first = self.columns[0].slots
        return sum(1 for s in first if s.r == 0)


# ---------------------------------------------------------------------------
# Value validation against primitives
# ---------------------------------------------------------------------------
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def _validate_primitive(node: Node, value: Any, loc: dict[str, Any]) -> Any:
    kind = node.primitive
    if kind in ("int32", "int64"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise error(
                ErrorCode.VALUE_TYPE_MISMATCH,
                f"field '{node.name}' expects {kind}, got {type(value).__name__}: {value!r}",
                field=node.name, expected=kind, actual=type(value).__name__, **loc,
            )
        lo, hi = (_INT32_MIN, _INT32_MAX) if kind == "int32" else (_INT64_MIN, _INT64_MAX)
        if not lo <= value <= hi:
            raise error(
                ErrorCode.VALUE_OUT_OF_RANGE,
                f"{kind} value {value} out of range [{lo}, {hi}]",
                field=node.name, value=value, **loc,
            )
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise error(
                ErrorCode.VALUE_TYPE_MISMATCH,
                f"field '{node.name}' expects boolean, got {type(value).__name__}: {value!r}",
                field=node.name, expected="boolean", actual=type(value).__name__, **loc,
            )
        return value
    if kind == "double":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise error(
                ErrorCode.VALUE_TYPE_MISMATCH,
                f"field '{node.name}' expects double, got {type(value).__name__}: {value!r}",
                field=node.name, expected="double", actual=type(value).__name__, **loc,
            )
        return float(value)
    if kind in ("string", "binary"):
        if not isinstance(value, str):
            raise error(
                ErrorCode.VALUE_TYPE_MISMATCH,
                f"field '{node.name}' expects {kind}, got {type(value).__name__}: {value!r}",
                field=node.name, expected=kind, actual=type(value).__name__, **loc,
            )
        return value
    raise error(
        ErrorCode.VALUE_TYPE_MISMATCH, f"unknown primitive kind {kind}",
        field=node.name, **loc,
    )


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------
class _Encoder:
    def __init__(self, schema: Schema):
        self.schema = schema
        self.columns = [LeafColumn(node=leaf) for leaf in schema.leaves]
        self._by_path = {".".join(leaf.node.path[1:]): col
                         for col in self.columns for leaf in (col,)}

    def _col(self, node: Node) -> LeafColumn:
        return self._by_path[".".join(node.path[1:])]

    def _emit(self, node: Node, d: int, r: int, value: Any) -> None:
        self._col(node).append(Slot(d=d, r=r, value=value))

    def encode(self, records: list[Any]) -> None:
        if not isinstance(records, list):
            raise error(ErrorCode.INVALID_REQUEST, "records must be a list")
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                raise error(
                    ErrorCode.VALUE_TYPE_MISMATCH,
                    f"top-level record {i} must be an object, got {type(rec).__name__}",
                    record=i,
                )
            # Root message is REQUIRED (parent_def=0); children live at the
            # root's own accumulated definition level (0 here).
            for child in self.schema.root.children:
                if child.name not in rec:
                    if child.nullable:
                        self._encode_node(None, child, 0, 0, {"record": i})
                    else:
                        raise error(
                            ErrorCode.NULL_IN_REQUIRED,
                            f"required top-level field '{child.name}' missing",
                            field=child.name, record=i,
                        )
                else:
                    self._encode_node(rec[child.name], child, 0, 0,
                                      {"record": i})

    def _encode_node(self, value: Any, node: Node, parent_d: int,
                     parent_r: int, loc: dict[str, Any]) -> None:
        if node.kind == "struct":
            self._encode_struct(value, node, parent_d, parent_r, loc)
        elif node.kind == "list":
            self._encode_list(value, node, parent_d, parent_r, loc)
        else:
            self._encode_primitive(value, node, parent_d, parent_r, loc)

    def _encode_primitive(self, value: Any, node: Node, parent_d: int,
                          parent_r: int, loc: dict[str, Any]) -> None:
        own_d = parent_d + (1 if node.nullable else 0)
        if value is None:
            if not node.nullable:
                raise error(
                    ErrorCode.NULL_IN_REQUIRED,
                    f"required field '{node.name}' received NULL",
                    field=node.name, **loc,
                )
            self._emit(node, own_d - 1, parent_r, NULL)
        else:
            v = _validate_primitive(node, value, loc)
            self._emit(node, own_d, parent_r, v)

    def _encode_struct(self, value: Any, node: Node, parent_d: int,
                       parent_r: int, loc: dict[str, Any]) -> None:
        own_d = parent_d + (1 if node.nullable else 0)
        if value is None:
            if not node.nullable:
                raise error(
                    ErrorCode.NULL_IN_REQUIRED,
                    f"required struct '{node.name}' received NULL",
                    field=node.name, **loc,
                )
            # One NULL marker per leaf, at the struct's own definition level
            # minus one.
            marker_d = own_d - 1
            for child in node.children:
                self._null_subtree(child, marker_d, parent_r, loc)
            return
        if not isinstance(value, dict):
            raise error(
                ErrorCode.VALUE_TYPE_MISMATCH,
                f"struct '{node.name}' expects object, got {type(value).__name__}",
                field=node.name, **loc,
            )
        names = {c.name for c in node.children}
        unknown = set(value) - names
        if unknown:
            raise error(
                ErrorCode.VALUE_TYPE_MISMATCH,
                f"struct '{node.name}' got unknown fields {sorted(unknown)}",
                field=node.name, unknown=sorted(unknown), **loc,
            )
        for child in node.children:
            if child.name not in value:
                if child.nullable:
                    self._encode_node(None, child, own_d, parent_r, loc)
                else:
                    raise error(
                        ErrorCode.NULL_IN_REQUIRED,
                        f"required field '{child.name}' missing from struct '{node.name}'",
                        field=child.name, **loc,
                    )
            else:
                self._encode_node(value[child.name], child, own_d, parent_r, loc)

    def _null_subtree(self, node: Node, marker_d: int, r: int,
                      loc: dict[str, Any]) -> None:
        """Emit NULL markers for every leaf under an absent optional node.

        The same definition level is carried by every leaf regardless of
        intermediate optional/repeated nodes (their contributions only
        distinguish levels *above* the absent node, not below it).
        """
        self._null_item(node, marker_d, r, loc)

    def _encode_list(self, value: Any, node: Node, parent_d: int,
                     parent_r: int, loc: dict[str, Any]) -> None:
        # Canonical 3-level LIST: outer group OPTIONAL, element REPEATED.
        outer_d = parent_d + 1
        element_d = outer_d + 1
        item = node.item
        if value is None:
            # NULL list: the optional outer group is absent -> the item
            # subtree leaves carry D = outer_d - 1 (= parent_d).
            self._null_item(item, outer_d - 1, parent_r, loc)
            return
        if not isinstance(value, list):
            raise error(
                ErrorCode.VALUE_TYPE_MISMATCH,
                f"list '{node.name}' expects array, got {type(value).__name__}",
                field=node.name, **loc,
            )
        if len(value) == 0:
            # Empty list: outer group present, zero REPEATED elements ->
            # item subtree leaves carry D = outer_d.
            self._null_item(item, outer_d, parent_r, loc)
            return
        for j, elem in enumerate(value):
            elem_loc = {**loc, "list_index": j}
            # First element inherits R from the surrounding context; later
            # elements repeat at this list's own repetition level.
            rep = parent_r if j == 0 else node.max_rep
            if elem is None:
                # NULL element: the REPEATED element exists but the item
                # value is absent. Mark the item subtree starting one level
                # below the item's entry definition level.
                self._null_element(item, element_d - 1, rep, elem_loc)
            else:
                self._encode_node(elem, item, element_d, rep, elem_loc)

    def _null_item(self, node: Node, marker_d: int, r: int,
                   loc: dict[str, Any]) -> None:
        """Mark all leaves of a subtree at the SAME definition level.

        Used for NULL/empty *lists*: the marker is the absent/empty group's
        level and every leaf beneath it carries it unchanged, however many
        intermediate struct/list groups exist.
        """
        if node.kind == "primitive":
            self._emit(node, marker_d, r, NULL)
        elif node.kind == "struct":
            for child in node.children:
                self._null_item(child, marker_d, r, loc)
        else:
            assert node.item is not None
            self._null_item(node.item, marker_d, r, loc)

    def _null_element(self, node: Node, d: int, r: int,
                      loc: dict[str, Any]) -> None:
        """Mark a NULL REPEATED list element on its subtree leaves.

        ``d`` is the element-group level (element_d - 1 of the owning list).
        Cases, matching the Parquet truth table:

        * primitive element NULL -> the REPEATED element exists, the leaf
          value is NULL -> marker at the leaf's max definition level;
        * struct element NULL -> struct presence rides on the element group,
          so every leaf is marked at ``d``;
        * nested-list element NULL -> the missing value is the whole inner
          list; its leaves sit at the inner list's absent-element level,
          d+1 (one OPTIONAL outer group deeper), NOT at its leaf max.
        """
        if node.kind == "primitive":
            # The REPEATED element exists; the leaf value is NULL -> marker
            # one below the leaf maximum definition level.
            self._emit(node, node.max_def - 1, r, NULL)
        elif node.kind == "struct":
            # A NULL struct element leaves every subtree leaf at the struct
            # item's own definition level (its def_cutoff == element_d), not
            # one below: the REPEATED element exists, the struct is null.
            marker = node.def_cutoff
            for child in node.children:
                self._null_item(child, marker, r, loc)
        else:
            assert node.item is not None
            # Whole inner list missing: mark item subtree at fixed d+1; do
            # not descend further raising levels.
            self._null_item(node.item, d + 1, r, loc)


def encode_table(schema: Schema, records: list[Any]) -> EncodedTable:
    enc = _Encoder(schema)
    enc.encode(records)
    table = EncodedTable(schema=schema, columns=enc.columns)
    _assert_column_alignment(table, len(records))
    return table


def _assert_column_alignment(table: EncodedTable, n_records: int) -> None:
    """Columns assembled independently must share identical record boundaries.

    Every leaf column contributes exactly one R==0 slot per top-level record
    (an absent whole record still carries one NULL marker with R=0).
    """
    counts = {}
    for col in table.columns:
        count = sum(1 for s in col.slots if s.r == 0)
        counts[".".join(col.node.path[1:])] = count
    bad = {p: c for p, c in counts.items() if c != n_records}
    if bad:
        raise error(
            ErrorCode.COLUMN_RECORD_BOUNDARY_MISMATCH,
            "leaf columns disagree on the number of R=0 record starts; "
            "record boundaries would be misaligned when assembling columns",
            expected_record_starts=n_records,
            actual=bad,
        )



# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------
class _Cursor:
    __slots__ = ("col", "pos")

    def __init__(self, col: LeafColumn):
        self.col = col
        self.pos = 0

    def peek(self) -> Optional[Slot]:
        return self.col.slots[self.pos] if self.pos < len(self.col.slots) else None

    def take(self) -> Slot:
        s = self.col.slots[self.pos]
        self.pos += 1
        return s


def decode_table(table: EncodedTable) -> list[Any]:
    cursors = {id(c.node): _Cursor(c) for c in table.columns}
    n = table.record_count
    out: list[Any] = []
    for i in range(n):
        record: dict[str, Any] = {}
        for child in table.schema.root.children:
            record[child.name] = _decode_value(
                child, cursors, inherited_d=0, inherited_r=0,
                record=i, at_element_start=True,
            )
        out.append(record)
    for cur in cursors.values():
        if cur.pos != len(cur.col.slots):
            raise error(
                ErrorCode.PAGE_INVARIANT_VIOLATION,
                "decoder left unconsumed slots on a leaf column",
                column=".".join(cur.col.node.path[1:]),
                remaining=len(cur.col.slots) - cur.pos,
            )
    return out


def _cur(node: Node, cursors: dict[int, _Cursor]) -> _Cursor:
    return cursors[id(node)]


def _first_leaf(node: Node) -> Node:
    if node.kind == "primitive":
        return node
    if node.kind == "struct":
        return _first_leaf(node.children[0])
    assert node.item is not None
    return _first_leaf(node.item)


def _decode_value(node: Node, cursors: dict[int, _Cursor], inherited_d: int,
                  inherited_r: int, record: int,
                  at_element_start: bool,
                  force_struct_null_d: Optional[int] = None) -> Any:
    """Decode one value of ``node``.

    ``inherited_d`` / ``inherited_r`` are the levels accumulated ABOVE this
    node. ``at_element_start`` is True exactly for the first slot of a list
    element. ``force_struct_null_d`` overrides a struct item's null D level
    (the REPEATED element group carries the null for a list-item struct).
    """
    loc = {"record": record}
    if node.kind == "primitive":
        slot = _cur(node, cursors).take()
        return None if slot.is_null_marker else slot.value
    if node.kind == "struct":
        own_d = inherited_d + (1 if node.nullable else 0)
        probe = _cur(_first_leaf(node), cursors).peek()
        if probe is None:
            raise error(
                ErrorCode.ROUNDTRIP_MISMATCH,
                f"struct '{node.name}' expected a slot but the stream ended",
                **loc,
            )
        null_d = force_struct_null_d if force_struct_null_d is not None \
            else own_d - 1
        if probe.d == null_d and (force_struct_null_d is not None or
                                   node.nullable):
            for child in node.children:
                _skip_value(child, cursors, loc)
            return None
        result: dict[str, Any] = {}
        for child in node.children:
            # A list inside a struct starts a fresh list whose first element
            # inherits the struct's start repetition level (which, for a
            # list-item struct, is the element's head R).
            child_at_start = child.kind == "list"
            result[child.name] = _decode_value(
                child, cursors, inherited_d=own_d, inherited_r=inherited_r,
                record=record, at_element_start=child_at_start,
            )
        return result
    return _decode_list(node, cursors, inherited_d, inherited_r, record,
                        at_element_start)


def _skip_value(node: Node, cursors: dict[int, _Cursor],
                loc: dict[str, Any]) -> None:
    """Consume the NULL marker subtree of an absent node (one marker/leaf)."""
    if node.kind == "primitive":
        slot = _cur(node, cursors).take()
        if not slot.is_null_marker:
            raise error(
                ErrorCode.STRUCT_CHILD_PRESENCE_MISMATCH,
                f"expected NULL marker for absent '{node.name}', got value",
                field=node.name, **loc,
            )
    elif node.kind == "struct":
        for child in node.children:
            _skip_value(child, cursors, loc)
    else:
        assert node.item is not None
        _skip_value(node.item, cursors, loc)


def _decode_list(node: Node, cursors: dict[int, _Cursor], parent_d: int,
                 parent_r: int, record: int, at_element_start: bool) -> Any:
    """Decode a LIST value from the current cursor position.

    The outer LIST group is OPTIONAL (outer_d), the element group REPEATED
    (element_d). ``node.element_rep`` is this list's own repetition level.

    The first element of this list starts at the inherited repetition level
    (parent_r); every later element starts exactly at element_rep. When this
    list is itself an item of an enclosing list, ``parent_r`` is that
    enclosing element's start R -- possibly higher than the enclosing list's
    repeat level, which is how a non-empty inner list carries the outer
    element's R on its first value.
    """
    item = node.item
    outer_d = parent_d + 1
    element_d = outer_d + 1
    element_rep = node.element_rep
    loc = {"record": record}
    head = _cur(_first_leaf(item), cursors).peek()

    if head is None:
        return None
    # A slot below the inherited R belongs to an enclosing list/record, not
    # to this list value.
    if head.r < parent_r:
        return None

    # NULL / empty list identified purely by definition level.
    if head.d < outer_d:
        _skip_value(item, cursors, loc)
        return None
    if head.d == outer_d:
        _skip_value(item, cursors, loc)
        return []

    items: list[Any] = []
    first = True
    while True:
        head = _cur(_first_leaf(item), cursors).peek()
        if head is None:
            break
        if first:
            # The first element starts at the inherited R (which may equal
            # element_rep for the outermost list or a higher inherited level
            # for a nested inner list).
            if head.r < parent_r:
                break
        else:
            # Later element of THIS list repeats at element_rep.
            if head.r < element_rep:
                break
        value = _decode_list_item(item, cursors, element_d, head,
                                  element_rep, record)
        items.append(value)
        first = False
    return items


def _decode_list_item(item: Node, cursors: dict[int, _Cursor],
                      element_d: int, head: Slot, element_rep: int,
                      record: int) -> Any:
    """Decode one REPEATED element, given its first slot ``head``."""
    loc = {"record": record}
    if item.kind == "primitive":
        slot = _cur(item, cursors).take()
        return None if slot.is_null_marker else slot.value
    if item.kind == "struct":
        # A NULL struct element carries its leaves at the REPEATED element
        # group level (element_d - 1); override the struct's own cutoff.
        return _decode_value(
            item, cursors, inherited_d=element_d, inherited_r=head.r,
            record=record, at_element_start=False,
            force_struct_null_d=element_d - 1,
        )
    # item is itself a list.
    assert item.item is not None
    inner_outer_d = element_d + 1
    if head.d < inner_outer_d:
        _skip_value(item.item, cursors, loc)
        return None
    if head.d == inner_outer_d:
        _skip_value(item.item, cursors, loc)
        return []
    return _decode_list(
        item, cursors, parent_d=element_d, parent_r=head.r,
        record=record, at_element_start=True,
    )
