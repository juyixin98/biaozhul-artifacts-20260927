"""Kernel value validation and explicit rejection tests."""
from __future__ import annotations

import pytest

from app.core.errors import ErrorCode, StructuredError
from app.core.kernel import encode_table
from app.core.schema import Schema


def test_int32_out_of_range_rejected():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "x", "type": "int32"}]})
    with pytest.raises(StructuredError) as ei:
        encode_table(sch, [{"x": 2 ** 31}])
    assert ei.value.code == ErrorCode.VALUE_OUT_OF_RANGE
    assert ei.value.location["field"] == "x"


def test_boolean_type_mismatch_rejected():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "ok", "type": "boolean"}]})
    with pytest.raises(StructuredError) as ei:
        encode_table(sch, [{"ok": "yes"}])
    assert ei.value.code == ErrorCode.VALUE_TYPE_MISMATCH


def test_null_in_required_field_rejected():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "id", "type": "int64", "nullable": False}]})
    with pytest.raises(StructuredError) as ei:
        encode_table(sch, [{"id": None}])
    assert ei.value.code == ErrorCode.NULL_IN_REQUIRED


def test_list_wrong_container_type_rejected():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "x", "type": "list", "item": {"type": "int32"}}]})
    with pytest.raises(StructuredError) as ei:
        encode_table(sch, [{"x": 5}])
    assert ei.value.code == ErrorCode.VALUE_TYPE_MISMATCH


def test_top_level_record_must_be_object():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "x", "type": "int32"}]})
    with pytest.raises(StructuredError) as ei:
        encode_table(sch, [5])
    assert ei.value.code == ErrorCode.VALUE_TYPE_MISMATCH


def test_unknown_struct_field_rejected():
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "s", "type": "struct", "children": [
            {"name": "a", "type": "int32"}]}]})
    with pytest.raises(StructuredError) as ei:
        encode_table(sch, [{"s": {"a": 1, "b": 2}}])
    assert ei.value.code == ErrorCode.VALUE_TYPE_MISMATCH
    assert "b" in ei.value.location["unknown"]


def test_column_alignment_error_on_manual_corruption():
    # If a leaf column were forced to have a different R=0 count, the
    # alignment guard must refuse it.
    from app.core.kernel import (
        EncodedTable, LeafColumn, Slot, _assert_column_alignment,
    )
    sch = Schema({"name": "root", "type": "struct", "children": [
        {"name": "a", "type": "int32"}, {"name": "b", "type": "int32"}]})
    # c1 covers one record (one R=0); c2 has a dangling continuation slot
    # with R=1 that no record start, i.e. a second phantom record marker.
    c1 = LeafColumn(node=sch.leaves[0], slots=[Slot(1, 0, 5)])
    c2 = LeafColumn(node=sch.leaves[1],
                    slots=[Slot(1, 0, 6), Slot(1, 0, 7)])
    with pytest.raises(StructuredError) as ei:
        _assert_column_alignment(EncodedTable(sch, [c1, c2]), 1)
    assert ei.value.code == ErrorCode.COLUMN_RECORD_BOUNDARY_MISMATCH
