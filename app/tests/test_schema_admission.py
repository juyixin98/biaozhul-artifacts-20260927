"""Schema admission tests: supported surface, level metadata, rejections."""
from __future__ import annotations

import pytest

from app.core.errors import ErrorCode, StructuredError
from app.core.schema import PRIMITIVES, Schema


def test_primitive_max_levels_match_spec():
    # nullable list<int32> leaf must be max_def=3, max_rep=1.
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "a", "type": "list",
         "item": {"name": "element", "type": "int32"}}]})
    leaf = sch.leaves[0]
    assert leaf.max_def == 3
    assert leaf.max_rep == 1
    assert leaf.path[1:] == ("a", "list", "element")


def test_nested_list_max_levels():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "b", "type": "list",
         "item": {"name": "element", "type": "list",
                   "item": {"name": "element", "type": "int32"}}}]})
    leaf = sch.leaves[0]
    assert (leaf.max_def, leaf.max_rep) == (5, 2)


def test_root_message_is_required():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "x", "type": "int32"}]})
    assert sch.root.nullable is False
    assert sch.root.max_def == 0


def test_primitive_types_admitted():
    children = [
        {"name": p, "type": t}
        for t, p in zip(PRIMITIVES, ["i", "l", "d", "b", "s", "by"])
    ]
    sch = Schema({"name": "root", "type": "struct", "children": children})
    assert {n.physical for n in sch.leaves} == {
        v[0] for v in PRIMITIVES.values()}


@pytest.mark.parametrize("bad_type", ["map", "decimal", "date", "timestamp",
                                      "uuid", "json", "enum", "float", "float16"])
def test_unsupported_logical_types_rejected(bad_type):
    with pytest.raises(StructuredError) as ei:
        Schema({"name": "root", "type": "struct", "children": [
            {"name": "x", "type": bad_type}]})
    assert ei.value.code == ErrorCode.UNSUPPORTED_LOGICAL_TYPE
    assert ei.value.location["logical_type"] == bad_type


def test_empty_struct_rejected():
    with pytest.raises(StructuredError) as ei:
        Schema({"name": "root", "type": "struct", "children": []})
    assert ei.value.code == ErrorCode.EMPTY_STRUCT


def test_legacy_two_level_list_rejected():
    with pytest.raises(StructuredError) as ei:
        Schema({"name": "root", "type": "struct", "children": [
            {"name": "x", "type": "list", "layout": "2level",
             "item": {"type": "int32"}}]})
    assert ei.value.code == ErrorCode.LEGACY_LIST_LAYOUT


def test_duplicate_field_names_rejected():
    with pytest.raises(StructuredError) as ei:
        Schema({"name": "root", "type": "struct", "children": [
            {"name": "x", "type": "int32"},
            {"name": "x", "type": "int64"}]})
    assert ei.value.code == ErrorCode.INVALID_SCHEMA


def test_missing_type_rejected():
    with pytest.raises(StructuredError) as ei:
        Schema({"name": "root", "type": "struct", "children": [
            {"name": "x"}]})
    assert ei.value.code == ErrorCode.INVALID_SCHEMA


def test_list_requires_item():
    with pytest.raises(StructuredError) as ei:
        Schema({"name": "root", "type": "struct", "children": [
            {"name": "x", "type": "list"}]})
    assert ei.value.code == ErrorCode.INVALID_SCHEMA
