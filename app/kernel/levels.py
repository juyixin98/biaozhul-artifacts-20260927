"""Definition/repetition level kernel (self-implemented, library independent).

This module contains the core mechanism: it walks a record tree and produces
the (definition_level, repetition_level, optional value) triples a Parquet
leaf column stores, and reconstructs the tree from those triples. It is
deliberately decoupled from bytes/IO so it can be unit tested directly and so
that PyArrow is never used to "implement" the expected answers -- expected
trees are hand written and the levels are produced here.

Semantics implemented (canonical 3-level Parquet LIST)::

    optional/required group <list> (LIST) {
      repeated group list {
        <element>
      }
    }

The three list states a record may be in:

* NULL list        -> outer LIST group absent,        DL = outer_present - 1
* empty list       -> outer present, repeated absent, DL = outer_present
* list with N elems -> N events on the leaf at the "list" repetition level,
                       first at the record's RL, subsequent at the inner RL

"list containing NULL" is an element whose DL marks the element group present
but the element itself absent. These states are therefore encoded as three
distinct DL values, which is exactly what makes them distinguishable on
read-back.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple, Sequence

from .schema import (
    LeafColumn, ListNode, OPTIONAL, PrimitiveNode, REQUIRED, RootNode,
    SchemaNode, StructNode,
)


class LeafEvent(NamedTuple):
    """One encoded slot of a leaf column: definition level, repetition level, value."""
    definition_level: int
    repetition_level: int
    value: Any  # Python scalar when the slot is a present value, else None


@dataclass(frozen=True)
class EncodedColumn:
    leaf: LeafColumn
    events: list[LeafEvent]


@dataclass
class EncodeResult:
    columns: list[EncodedColumn]
    num_records: int
    # Per-record presence of structs with zero physical fields. Such structs
    # have no leaf column and are otherwise invisible, so their presence is
    # carried out-of-band as dotted path -> record indices.
    empty_struct_present: dict[str, list[int]]


# --------------------------------------------------------------------------- #
# Encode: tree -> levels
# --------------------------------------------------------------------------- #
#
# Repetition level convention (fixed per repeated-group depth, Parquet spec):
#
#   entry_rl   -- RL at which THIS call's first element appears (0 at record
#                 root, or the enclosing repeated group's depth for elements)
#   rep_depth  -- depth of this list's own repeated group (entry_rl + 1);
#                 every element after the first repeats at exactly rep_depth.
#
# This is what makes [[1,2]] (RLs 0,2) distinguishable from [[1],[2]] (RLs 0,1).

def encode_records(root: RootNode, records: Sequence[dict[str, Any]]) -> EncodeResult:
    leaves = {c.path: EncodedColumn(c, []) for c in root_schema_leaves(root)}
    empty_struct_present: dict[str, list[int]] = {}

    def mark_empty(path: tuple[str, ...], rec_idx: int) -> None:
        empty_struct_present.setdefault(".".join(path), []).append(rec_idx)

    for rec_idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise ValueError(f"record {rec_idx}: top-level value must be an object")
        for field in root.fields:
            if field.name not in rec:
                if field.repetition == REQUIRED:
                    raise ValueError(
                        f"record {rec_idx}: required field {field.name!r} is missing")
                value, absent = None, True
            else:
                value, absent = rec[field.name], rec[field.name] is None
            _emit_node(field, value, absent, cur_dl=0, entry_rl=0,
                       path=(field.name,), columns=leaves,
                       rec_idx=rec_idx, on_empty_struct=mark_empty)

    return EncodeResult(
        columns=[leaves[c.path] for c in root_schema_leaves(root)],
        num_records=len(records),
        empty_struct_present=empty_struct_present,
    )


def root_schema_leaves(root: RootNode) -> list[LeafColumn]:
    from .schema import leaf_columns
    return leaf_columns(root)


def _emit_node(node: SchemaNode, value: Any, absent: bool,
               cur_dl: int, entry_rl: int,
               path: tuple[str, ...], columns: dict[tuple[str, ...], EncodedColumn],
               rec_idx: int, on_empty_struct) -> None:
    if isinstance(node, PrimitiveNode):
        if absent:
            if node.repetition == REQUIRED and cur_dl == 0 and entry_rl == 0:
                raise ValueError(
                    f"record {rec_idx}: required field {'.'.join(path)} is NULL")
            columns[path].events.append(LeafEvent(cur_dl, entry_rl, None))
        else:
            columns[path].events.append(
                LeafEvent(cur_dl + (1 if node.repetition == OPTIONAL else 0),
                          entry_rl, value))
        return

    if isinstance(node, StructNode):
        if node.repetition == OPTIONAL:
            if absent:
                _emit_struct_null(node, cur_dl, entry_rl, path, columns)
                return
            present_dl = cur_dl + 1
        else:
            if absent:
                raise ValueError(
                    f"record {rec_idx}: required struct {'.'.join(path)} is NULL")
            present_dl = cur_dl
        if node.is_empty:
            on_empty_struct(path, rec_idx)
            return
        if not isinstance(value, dict):
            raise ValueError(
                f"record {rec_idx}: struct {'.'.join(path)} must be an object or NULL")
        for child in node.fields:
            child_absent = child.name not in value or value[child.name] is None
            _emit_node(child, value.get(child.name), child_absent,
                       present_dl, entry_rl,
                       path + (child.name,), columns, rec_idx, on_empty_struct)
        return

    if isinstance(node, ListNode):
        _emit_list(node, value, absent, cur_dl, entry_rl,
                   path, columns, rec_idx, on_empty_struct)
        return

    raise TypeError(f"unsupported node {node!r}")


def _emit_struct_null(node: StructNode, null_dl: int, cur_rl: int,
                      path: tuple[str, ...],
                      columns: dict[tuple[str, ...], EncodedColumn]) -> None:
    """A NULL struct contributes one event per descendant leaf at null_dl."""
    for child in node.fields:
        child_path = path + (child.name,)
        if isinstance(child, PrimitiveNode):
            columns[child_path].events.append(LeafEvent(null_dl, cur_rl, None))
        elif isinstance(child, StructNode):
            if child.is_empty:
                continue
            _emit_struct_null(child, null_dl, cur_rl, child_path, columns)
        elif isinstance(child, ListNode):
            # When the enclosing struct is NULL the inner list state is
            # invisible; one placeholder lands at the struct's DL.
            _emit_list_column_null(child, null_dl, cur_rl, child_path, columns)


def _emit_list_column_null(node: ListNode, null_dl: int, cur_rl: int,
                           path: tuple[str, ...],
                           columns: dict[tuple[str, ...], EncodedColumn]) -> None:
    elem_path = path + ("list", node.element.name)
    if isinstance(node.element, PrimitiveNode):
        columns[elem_path].events.append(LeafEvent(null_dl, cur_rl, None))
    elif isinstance(node.element, StructNode):
        if node.element.is_empty:
            return
        for leaf in _struct_leaf_paths(node.element, elem_path):
            columns[leaf].events.append(LeafEvent(null_dl, cur_rl, None))
    elif isinstance(node.element, ListNode):
        _emit_list_column_null(node.element, null_dl, cur_rl, elem_path, columns)


def _struct_leaf_paths(node: StructNode, prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for child in node.fields:
        p = prefix + (child.name,)
        if isinstance(child, PrimitiveNode):
            out.append(p)
        elif isinstance(child, StructNode) and not child.is_empty:
            out.extend(_struct_leaf_paths(child, p))
        elif isinstance(child, ListNode):
            out.extend(_list_leaf_paths(child, p))
    return out


def _list_leaf_paths(node: ListNode, prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
    elem_path = prefix + ("list", node.element.name)
    if isinstance(node.element, PrimitiveNode):
        return [elem_path]
    if isinstance(node.element, StructNode):
        return _struct_leaf_paths(node.element, elem_path)
    return _list_leaf_paths(node.element, elem_path)


def _emit_list(node: ListNode, value: Any, absent: bool,
               cur_dl: int, entry_rl: int,
               path: tuple[str, ...], columns: dict[tuple[str, ...], EncodedColumn],
               rec_idx: int, on_empty_struct,
               forced_rep_depth: int | None = None) -> None:
    # DL ladder for this repeated group:
    outer_dl = cur_dl + (1 if node.repetition == OPTIONAL else 0)
    list_dl = outer_dl + 1          # repeated group present (element slot exists)
    rep_depth = (entry_rl + 1) if forced_rep_depth is None else forced_rep_depth

    if absent:
        _emit_list_column_null(node, cur_dl, entry_rl, path, columns)
        return
    if not isinstance(value, list):
        raise ValueError(
            f"record {rec_idx}: list {'.'.join(path)} must be an array or NULL")
    if len(value) == 0:
        _emit_list_column_null(node, outer_dl, entry_rl, path, columns)
        return

    element = node.element
    for i, item in enumerate(value):
        # First element repeats at the entry RL; later ones at this group's
        # fixed absolute depth. The nested list below receives the per-item RL
        # as its entry but its own repeated group stays at rep_depth + 1.
        item_rl = entry_rl if i == 0 else rep_depth
        item_absent = item is None
        if isinstance(element, PrimitiveNode):
            elem_path = path + ("list", element.name)
            if item_absent:
                columns[elem_path].events.append(LeafEvent(list_dl, item_rl, None))
            else:
                columns[elem_path].events.append(
                    LeafEvent(list_dl + 1, item_rl, item))
        elif isinstance(element, StructNode):
            if element.is_empty:
                on_empty_struct(path + ("list", element.name), rec_idx)
                continue
            _emit_struct_element(element, item, item_absent, list_dl, item_rl,
                                 path + ("list", element.name),
                                 columns, rec_idx, on_empty_struct)
        elif isinstance(element, ListNode):
            # The inner repeated group's fixed depth is THIS group's depth + 1,
            # independent of the current item's entry RL.
            _emit_list(element, item, item_absent, list_dl, item_rl,
                       path + ("list", element.name),
                       columns, rec_idx, on_empty_struct,
                       forced_rep_depth=rep_depth + 1)


def _emit_struct_element(node: StructNode, value: Any, absent: bool,
                         cur_dl: int, cur_rl: int,
                         path: tuple[str, ...], columns, rec_idx,
                         on_empty_struct) -> None:
    if absent:
        _emit_struct_null(node, cur_dl, cur_rl, path, columns)
        return
    if not isinstance(value, dict):
        raise ValueError(
            f"record {rec_idx}: struct element {'.'.join(path)} must be object or NULL")
    present_dl = cur_dl + 1  # element group present
    for child in node.fields:
        child_absent = child.name not in value or value[child.name] is None
        _emit_node(child, value.get(child.name), child_absent,
                   present_dl, cur_rl,
                   path + (child.name,), columns, rec_idx, on_empty_struct)

# --------------------------------------------------------------------------- #
# Decode: levels -> tree
# --------------------------------------------------------------------------- #

@dataclass
class DecodeDiagnostic:
    column_path: str
    page_index: int
    position_in_page: int
    event_index: int
    detail: str


class LevelDecodeError(ValueError):
    """Malformed levels; carries structured locations (page index + position)."""

    def __init__(self, message: str, diagnostics: list[DecodeDiagnostic] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or []


def decode_records(root: RootNode, columns: Sequence[EncodedColumn],
                   num_records: int | None = None,
                   empty_struct_present: dict[str, list[int]] | None = None
                   ) -> list[dict[str, Any]]:
    """Reconstruct record trees from per-leaf event streams.

    Each leaf's events are first split into per-record slices on RL == 0, so
    column composition aligns on the same record boundary. Within one record a
    cursor walks every leaf slice in schema order. ``empty_struct_present``
    carries out-of-band presence for zero-field structs (no physical column).
    """
    empty_struct_present = empty_struct_present or {}
    by_path = {tuple(c.leaf.path): c for c in columns}
    expected_leaves = root_schema_leaves(root)
    missing = [c for c in expected_leaves if c.path not in by_path]
    if missing:
        raise LevelDecodeError(
            "missing leaf column(s): " + ", ".join(".".join(c.path) for c in missing))

    per_column_records: dict[tuple[str, ...], list[list[LeafEvent]]] = {}
    record_count: int | None = None
    diagnostics: list[DecodeDiagnostic] = []
    for leaf in expected_leaves:
        col = by_path[leaf.path]
        slices = _split_record_slices(leaf, col.events)
        if record_count is None:
            record_count = len(slices)
        elif len(slices) != record_count:
            diagnostics.append(DecodeDiagnostic(
                column_path=".".join(leaf.path), page_index=-1,
                position_in_page=-1, event_index=len(col.events),
                detail=f"column yields {len(slices)} records, expected {record_count}"))
        per_column_records[leaf.path] = slices

    if diagnostics:
        raise LevelDecodeError("record boundary mismatch across columns", diagnostics)
    if num_records is not None and record_count != num_records:
        raise LevelDecodeError(
            f"record count {record_count} does not match declared {num_records}")
    assert record_count is not None

    empty_present_sets = {p: set(rows) for p, rows in empty_struct_present.items()}
    records: list[dict[str, Any]] = []
    for r in range(record_count):
        reader = _RecordReader(
            {p: evs[r] for p, evs in per_column_records.items()}, r,
            empty_present_sets)
        rec: dict[str, Any] = {}
        for field in root.fields:
            rec[field.name] = reader.read_node(field, base_dl=0, base_rl=0,
                                               path=(field.name,),
                                               empty_set=empty_present_sets)
        leftovers = reader.leftover_events()
        if leftovers:
            p, idx, ev = leftovers[0]
            raise LevelDecodeError(
                f"record {r}: unconsumed events in column {'.'.join(p)} "
                f"(at slot {idx}, RL={ev.repetition_level}, DL={ev.definition_level})",
                [DecodeDiagnostic(".".join(p), -1, idx, idx, "unconsumed event")])
        records.append(rec)
    return records


def _split_record_slices(leaf: LeafColumn,
                         events: Sequence[LeafEvent]) -> list[list[LeafEvent]]:
    slices: list[list[LeafEvent]] = []
    current: list[LeafEvent] | None = None
    for idx, ev in enumerate(events):
        if ev.repetition_level == 0:
            current = []
            slices.append(current)
        elif current is None:
            raise LevelDecodeError(
                f"{'.'.join(leaf.path)}: event #{idx} has RL={ev.repetition_level} "
                "before any record start (RL=0)",
                [DecodeDiagnostic(".".join(leaf.path), -1, idx, idx,
                                  "leading event without RL=0")])
        current.append(ev)
    return slices


class _RecordReader:
    """Cursor over one record's events for every leaf column."""

    def __init__(self, per_column: dict[tuple[str, ...], list[LeafEvent]],
                 record_index: int,
                 empty_present: dict[str, set[int]] | None = None):
        self._streams = {p: list(evs) for p, evs in per_column.items()}
        self.record_index = record_index
        self._empty_present = empty_present or {}

    # -- primitive cursor operations --------------------------------------- #
    def _peek(self, path: tuple[str, ...]) -> LeafEvent:
        stream = self._streams.get(path)
        if stream is None:
            raise LevelDecodeError(f"internal: unknown leaf path {'.'.join(path)}")
        if not stream:
            raise LevelDecodeError(
                f"record {self.record_index}: column {'.'.join(path)} exhausted "
                "while schema expects more data",
                [DecodeDiagnostic(".".join(path), -1, -1, -1,
                                  "unexpected end of column events")])
        return stream[0]

    def _take(self, path: tuple[str, ...]) -> LeafEvent:
        ev = self._peek(path)
        self._streams[path].pop(0)
        return ev

    def leftover_events(self):
        return [(p, len(evs), evs[0]) for p, evs in self._streams.items() if evs]

    # -- schema-driven walk ------------------------------------------------- #
    def read_node(self, node: SchemaNode, base_dl: int, base_rl: int,
                  path: tuple[str, ...], empty_set: set[str]) -> Any:
        if isinstance(node, PrimitiveNode):
            return self._read_primitive(node, base_dl, base_rl, path)
        if isinstance(node, StructNode):
            return self._read_struct(node, base_dl, base_rl, path, empty_set)
        if isinstance(node, ListNode):
            return self._read_list(node, base_dl, base_rl, path, empty_set)
        raise TypeError(node)

    def _read_primitive(self, node: PrimitiveNode, base_dl: int, base_rl: int,
                        path: tuple[str, ...]) -> Any:
        ev = self._take(path)
        if ev.repetition_level != base_rl:
            raise LevelDecodeError(
                f"record {self.record_index}: {'.'.join(path)} unexpected RL "
                f"{ev.repetition_level}, want {base_rl}",
                [DecodeDiagnostic(".".join(path), -1, -1, -1,
                                  f"RL mismatch {ev.repetition_level}!={base_rl}")])
        present_dl = base_dl + (1 if node.repetition == OPTIONAL else 0)
        if ev.definition_level < present_dl:
            return None
        return ev.value

    def _read_struct(self, node: StructNode, base_dl: int, base_rl: int,
                     path: tuple[str, ...], empty_set: set[str]) -> Any:
        if node.is_empty:
            # No physical representation; per-record presence is out-of-band.
            rows = self._empty_present.get(".".join(path))
            return {} if rows is not None and self.record_index in rows else None
        if node.repetition == OPTIONAL:
            probe = _first_leaf_path(node, path)
            dl = self._peek(probe).definition_level
            if dl < base_dl + 1:
                # NULL struct: consume exactly one placeholder per descendant leaf.
                self._drain_struct(node, base_dl, base_rl, path, probe_dl=dl)
                return None
            struct_dl = base_dl + 1
        else:
            struct_dl = base_dl
        out: dict[str, Any] = {}
        for child in node.fields:
            out[child.name] = self.read_node(child, struct_dl, base_rl,
                                             path + (child.name,), empty_set)
        return out

    def _drain_struct(self, node: StructNode, null_dl: int, base_rl: int,
                      path: tuple[str, ...], probe_dl: int) -> None:
        for child in node.fields:
            self._drain_node(child, null_dl, base_rl, path + (child.name,))

    def _drain_node(self, node: SchemaNode, null_dl: int, base_rl: int,
                    path: tuple[str, ...]) -> None:
        """Consume one NULL-ancestor placeholder emitted under a null struct."""
        if isinstance(node, PrimitiveNode):
            ev = self._take(path)
            if ev.definition_level != null_dl:
                raise LevelDecodeError(
                    f"record {self.record_index}: {'.'.join(path)} placeholder DL "
                    f"{ev.definition_level} != {null_dl}")
        elif isinstance(node, StructNode):
            if node.is_empty:
                return
            for child in node.fields:
                self._drain_node(child, null_dl, base_rl, path + (child.name,))
        elif isinstance(node, ListNode):
            # Null ancestor: one placeholder on each descendant leaf at null_dl.
            for leaf_path in _list_all_leaf_paths(node, path):
                ev = self._take(leaf_path)
                if ev.definition_level != null_dl:
                    raise LevelDecodeError(
                        f"record {self.record_index}: {'.'.join(leaf_path)} "
                        f"placeholder DL {ev.definition_level} != {null_dl}")

    def _read_list(self, node: ListNode, base_dl: int, base_rl: int,
                   path: tuple[str, ...], empty_set: set[str]) -> Any:
        """Read a list at repetition level ``base_rl + 1``.

        Sequential cursor algorithm (mirrors the encoder): the first element is
        read at the entry RL ``base_rl``; a subsequent element exists while the
        next probe event repeats at exactly this group's level
        (``base_rl + 1``). A lower RL means a new outer element/record.
        """
        outer_dl = base_dl + (1 if node.repetition == OPTIONAL else 0)
        list_dl = outer_dl + 1
        rep_depth = base_rl + 1
        elem_path = path + ("list", node.element.name)
        element = node.element
        probe = _element_probe_path(element, elem_path)
        ev0 = self._peek(probe)

        if ev0.definition_level < outer_dl:
            self._drain_node(element, ev0.definition_level, base_rl, elem_path)
            return None
        if ev0.definition_level == outer_dl:
            self._drain_node(element, outer_dl, base_rl, elem_path)
            return []

        result: list[Any] = []
        first = True
        while True:
            item_rl = base_rl if first else rep_depth
            result.append(self._read_element(
                element, list_dl, entry_rl=item_rl,
                group_rep_depth=rep_depth, next_rep_depth=rep_depth + 1,
                path=elem_path, empty_set=empty_set))
            first = False
            nxt = self._maybe_peek(probe)
            if nxt is None or nxt.repetition_level != rep_depth:
                break
        return result

    def _maybe_peek(self, path: tuple[str, ...]) -> LeafEvent | None:
        stream = self._streams.get(path)
        if stream is None:
            return None
        return stream[0] if stream else None

    def _read_element(self, node: SchemaNode, list_dl: int,
                      entry_rl: int, group_rep_depth: int,
                      next_rep_depth: int,
                      path: tuple[str, ...], empty_set: set[str]) -> Any:
        """Read one element of a repeated group at repetition level entry_rl.

        ``group_rep_depth`` is this repeated group's fixed RL (continuation RL
        of sibling elements); ``next_rep_depth`` is the fixed RL a nested
        repeated group one level deeper will use.
        """
        if isinstance(node, PrimitiveNode):
            ev = self._take(path)
            if ev.repetition_level != entry_rl:
                raise LevelDecodeError(
                    f"record {self.record_index}: {'.'.join(path)} element RL "
                    f"{ev.repetition_level} != {entry_rl}",
                    [DecodeDiagnostic(".".join(path), -1, -1, -1,
                                      f"RL {ev.repetition_level}!={entry_rl}")])
            return None if ev.definition_level < list_dl + 1 else ev.value

        if isinstance(node, StructNode):
            probe = _first_leaf_path(node, path)
            pev = self._peek(probe)
            if pev.definition_level < list_dl + 1:
                # NULL struct element: placeholder on every descendant leaf.
                self._drain_node(node, list_dl, entry_rl, path)
                return None
            out: dict[str, Any] = {}
            elem_dl = list_dl + 1
            for child in node.fields:
                out[child.name] = self.read_node(child, elem_dl, entry_rl,
                                                 path + (child.name,), empty_set)
            return out

        if isinstance(node, ListNode):
            return self._read_nested_list(
                node, list_dl, entry_rl=entry_rl,
                outer_rep_depth=group_rep_depth,
                inner_rep_depth=next_rep_depth,
                path=path, empty_set=empty_set)
        raise TypeError(node)

    def _read_nested_list(self, node: ListNode, list_dl: int,
                          entry_rl: int, outer_rep_depth: int,
                          inner_rep_depth: int,
                          path: tuple[str, ...], empty_set: set[str]) -> Any:
        """Read an element that is itself a list.

        ``entry_rl``       RL at which this inner list slot opens
                           (0 for the first outer element, outer_rep_depth for
                           later ones).
        ``outer_rep_depth`` fixed RL of the enclosing repeated group -- a next
                           event at this RL is a NEW outer element, not ours.
        ``inner_rep_depth`` fixed RL of THIS inner repeated group; our own
                           elements continue while the next event sits at it.
        """
        outer_dl = list_dl + 1       # inner optional LIST group present
        nested_list_dl = outer_dl + 1
        elem_path = path + ("list", node.element.name)
        probe = _element_probe_path(node.element, elem_path)
        ev0 = self._peek(probe)

        if ev0.definition_level < outer_dl:
            self._drain_node(node.element, ev0.definition_level,
                             ev0.repetition_level, elem_path)
            return None
        if ev0.definition_level == outer_dl:
            self._drain_node(node.element, outer_dl, ev0.repetition_level,
                             elem_path)
            return []

        result: list[Any] = []
        first = True
        while True:
            # First inner element of a populated inner list inherits the outer
            # slot's entry RL (e.g. the second outer element's first inner
            # value repeats at RL=1, not RL=2); subsequent inner values repeat
            # at this inner group's fixed depth. NULL/empty inner markers also
            # sit at the entry RL.
            if first:
                slot_rl = entry_rl
            else:
                slot_rl = inner_rep_depth
            result.append(self._read_element(
                node.element, nested_list_dl, entry_rl=slot_rl,
                group_rep_depth=inner_rep_depth,
                next_rep_depth=inner_rep_depth + 1,
                path=elem_path, empty_set=empty_set))
            first = False
            nxt = self._maybe_peek(probe)
            # Continue only on another value at THIS inner group's fixed RL.
            # outer_rep_depth is the fixed RL of the enclosing repeated group;
            # an event at or below it starts a new outer slot, so stop here.
            if nxt is None or nxt.repetition_level != inner_rep_depth:
                break
        return result

def _element_probe_path(node: SchemaNode, path: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(node, PrimitiveNode):
        return path
    if isinstance(node, StructNode):
        return _first_leaf_path(node, path)
    if isinstance(node, ListNode):
        return _list_first_path(node, path)
    raise TypeError(node)


def _first_leaf_path(node: StructNode, prefix: tuple[str, ...]) -> tuple[str, ...]:
    for child in node.fields:
        p = prefix + (child.name,)
        if isinstance(child, PrimitiveNode):
            return p
        if isinstance(child, StructNode) and not child.is_empty:
            return _first_leaf_path(child, p)
        if isinstance(child, ListNode):
            return _list_first_path(child, p)
    raise LevelDecodeError(f"{'.'.join(prefix)}: struct with no physical leaves")


def _list_first_path(node: ListNode, prefix: tuple[str, ...]) -> tuple[str, ...]:
    return _element_probe_path(node.element, prefix + ("list", node.element.name))


def _list_all_leaf_paths(node: ListNode, prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
    return _all_element_paths(node.element, prefix + ("list", node.element.name))


def _all_element_paths(node: SchemaNode, path: tuple[str, ...]) -> list[tuple[str, ...]]:
    if isinstance(node, PrimitiveNode):
        return [path]
    if isinstance(node, StructNode):
        out: list[tuple[str, ...]] = []
        for child in node.fields:
            out.extend(_all_element_paths(child, path + (child.name,)))
        return out
    if isinstance(node, ListNode):
        return _all_element_paths(node.element, path + ("list", node.element.name))
    raise TypeError(node)
