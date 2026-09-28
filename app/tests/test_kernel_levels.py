"""Kernel definition/repetition-level tests against hand-written fixtures.

The expected D/R streams are authored in fixtures/expected_trees.json (and the
multi-level ones cross-checked against the Parquet spec / fastparquet), so a
passing test is NOT the kernel grading itself.
"""
from __future__ import annotations

import pytest

from app.core.kernel import decode_table, encode_table
from app.core.schema import Schema


def _levels(table, path: str) -> dict[str, list[int]]:
    for c in table.columns:
        if ".".join(c.node.path[1:]) == path:
            return {"definition": [s.d for s in c.slots],
                    "repetition": [s.r for s in c.slots]}
    raise KeyError(path)


def test_nullable_list_levels(case):
    c = case("nullable_list_int32")
    sch = Schema(c["schema"])
    table = encode_table(sch, c["records"])
    assert _levels(table, "a.list.element") == \
        c["expected_levels"]["a.list.element"]
    # The semantic distinctions the contract calls out:
    slots = table.columns[0].slots
    # value/null-in-list/empty/null-list
    assert [(s.d, s.r) for s in slots] == [(3, 0), (2, 1), (3, 1),
                                           (1, 0), (0, 0)]


def test_nested_list_levels(case):
    c = case("nested_list_list_int32")
    sch = Schema(c["schema"])
    table = encode_table(sch, c["records"])
    assert _levels(table, "b.list.element.list.element") == \
        c["expected_levels"]["b.list.element.list.element"]


def test_three_level_list_levels(case):
    c = case("three_level_list")
    sch = Schema(c["schema"])
    table = encode_table(sch, c["records"])
    path = "c.list.element.list.element.list.element"
    assert _levels(table, path) == c["expected_levels"][path]


@pytest.mark.parametrize("case_name", [
    "nullable_list_int32", "nested_list_list_int32", "three_level_list",
])
def test_kernel_roundtrip_equals_handwritten_tree(case, case_name):
    c = case(case_name)
    sch = Schema(c["schema"])
    decoded = decode_table(encode_table(sch, c["records"]))
    assert decoded == c["records"]


def test_null_struct_element_materialised_like_pyarrow(case, tmp_path):
    # A NULL struct (standalone or a list element) carries no dedicated
    # null object in Parquet: PyArrow assembles it back as a struct whose
    # fields are all NULL. The kernel preserves ``None`` internally, so the
    # comparison applies the documented dialect normalisation (which the
    # verifier also performs and reports as a DIALECT finding).
    import pyarrow.parquet as pq
    from app.adapters.pyarrow_adapter import build_arrow_table
    from app.core.verifier import normalize_dialect
    c = case("consecutive_nulls_and_structs")
    sch = Schema(c["schema"])
    decoded = decode_table(encode_table(sch, c["records"]))
    pq.write_table(build_arrow_table(sch, c["records"]),
                   tmp_path / "s.parquet")
    arrow = pq.read_table(tmp_path / "s.parquet").to_pylist()
    for child in sch.root.children:
        name = child.name
        k = [normalize_dialect(r[name], child) for r in decoded]
        assert k == [r[name] for r in arrow]


def test_distinguish_empty_vs_null_list():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "x", "type": "list", "item": {"type": "int32"}}]})
    t = encode_table(sch, [{"x": []}, {"x": None}])
    empty_slot, null_slot = t.columns[0].slots
    assert (empty_slot.d, empty_slot.r) == (1, 0)
    assert (null_slot.d, null_slot.r) == (0, 0)
    assert decode_table(t) == [{"x": []}, {"x": None}]


def test_null_list_vs_null_element_inside():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "x", "type": "list",
         "item": {"type": "list", "item": {"type": "int32"}}}]})
    recs = [{"x": [None]}, {"x": None}, {"x": [[]]}, {"x": [[None]]}]
    t = encode_table(sch, recs)
    d = [s.d for s in t.columns[0].slots]
    r = [s.r for s in t.columns[0].slots]
    # NULL inner element D=2; NULL outer list D=0; empty inner list D=3;
    # null leaf INSIDE the single inner list D=4; its first element inherits
    # the record R=0 (no preceding repetition in a one-record table).
    assert d == [2, 0, 3, 4]
    assert r == [0, 0, 0, 0]
    assert decode_table(t) == recs
