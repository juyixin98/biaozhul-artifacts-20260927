"""Unit tests: zero-copy slice correctness, pointer identity, ownership safety.

Key reviewer requirements covered here:

* slices with non-zero offset keep NULL tests and element indexing correct,
  including when the offset crosses bitmap byte boundaries;
* slices share the exact physical buffer addresses with the parent (measured,
  not assumed);
* a view retains ownership of its buffers, so releasing the object the caller
  imported from cannot leave dangling reads.
"""
from __future__ import annotations

import gc
import struct

import pyarrow as pa
import pytest

from app.adapters.descriptor import descriptor_to_raw
from app.adapters.importer import import_raw
from app.core.bitmath import pack_validity
from app.core.columnview import ColumnView
from app.core.layout import RawColumnBuffers
from app.validation.checks import validate
from tests.fixtures.oracle import expected_fixed, expected_strings, fixed_descriptor, string_descriptor

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- import agree
@pytest.mark.parametrize("type_name,values", [
    ("int32", [10, -20, 30, None, 50, None, 70, 80, 90]),
    ("int64", [1, None, None, 4, 5]),
    ("uint8", list(range(9))),
    ("float64", [1.25, None, 3.5, 4.0]),
])
def test_imported_view_matches_pyarrow_and_oracle(type_name, values):
    desc = fixed_descriptor(type_name, values)
    raw = descriptor_to_raw(desc)
    report = validate(raw)
    assert report.ok
    result = import_raw(raw, validation_values=report.values)
    view = result.view

    # Independent oracle.
    assert view.to_pylist() == expected_fixed(type_name, values)
    # PyArrow decode of the same physical memory.
    assert view.materialize().to_pylist() == pa.array(
        [v for v in values], type=getattr(pa, type_name)()).to_pylist()
    assert result.evidence.agreement is True


def test_imported_strings_match_pyarrow_and_oracle():
    values = ["alpha", "", None, "dd", "eeee", None, "g", "hh", "i"]
    desc = string_descriptor(values)
    raw = descriptor_to_raw(desc)
    report = validate(raw)
    assert report.ok, report.failure_categories
    result = import_raw(raw, validation_values=report.values)
    assert result.view.to_pylist() == expected_strings(values)
    assert result.view.materialize().to_pylist() == pa.array(values, pa.utf8()).to_pylist()


# ------------------------------------------------------- non-zero-offset slice
def test_slice_nonzero_offset_nulls_and_indices_primitive():
    values = [10, 11, 12, 13, None, 15, None, 17, 18, 19]
    view = import_raw(descriptor_to_raw(fixed_descriptor("int32", values))).view
    sub = view.slice(3, 6)  # logical [13, None, 15, None, 17, 18]

    assert sub.offset == 3 and sub.length == 6
    assert sub.to_pylist() == [13, None, 15, None, 17, 18]
    assert [sub.is_null(i) for i in range(6)] == [False, True, False, True, False, False]
    # PyArrow must agree over the sliced window.
    assert sub.materialize().to_pylist() == pa.array(values, pa.int32()).slice(3, 6).to_pylist()
    # And every buffer address is shared (true zero copy).
    assert all(sub.shares_memory_with(view).values())


def test_slice_across_bitmap_byte_boundary_offset_7():
    # 16 elements, NULL at physical index 7 (last bit of byte 0) and 9.
    values = [i * 10 for i in range(16)]
    values[7] = None
    values[9] = None
    view = import_raw(descriptor_to_raw(fixed_descriptor("int16", values))).view
    sub = view.slice(7, 6)  # physical 7..12 -> [None,80,None,100,110,120]
    expected = [None, 80, None, 100, 110, 120]
    assert sub.to_pylist() == expected
    assert [sub.is_null(i) for i in range(6)] == [True, False, True, False, False, False]
    assert sub.materialize().to_pylist() == pa.array(values, pa.int16()).slice(7, 6).to_pylist()
    # is_null on the NULL exactly at the old byte boundary.
    assert sub.is_null(0) is True and sub.is_null(2) is True and sub.is_null(1) is False


def test_slice_of_slice_string_nonzero_offset():
    values = ["alpha", "", None, "dd", "eeee", None, "g", "hh"]
    view = import_raw(descriptor_to_raw(string_descriptor(values))).view
    sub = view.slice(2, 5)          # [None, 'dd', 'eeee', None, 'g']
    sub2 = sub.slice(1, 3)          # ['dd', 'eeee', None]
    assert sub2.offset == 3 and sub2.length == 3
    assert sub2.to_pylist() == ["dd", "eeee", None]
    assert [sub2.is_null(i) for i in range(3)] == [False, False, True]
    assert sub2.value(0) == "dd" and sub2.value(1) == "eeee" and sub2.value(2) is None
    assert sub2.materialize().to_pylist() == pa.array(values, pa.utf8()).slice(3, 3).to_pylist()
    # offsets/data/validity all still the parent's physical buffers.
    assert all(sub2.shares_memory_with(view).values())


def test_slice_out_of_range_is_categorized():
    from app.errors import LayoutError
    view = import_raw(descriptor_to_raw(fixed_descriptor("int32", [1, 2, 3]))).view
    with pytest.raises(LayoutError) as exc:
        view.slice(2, 5)
    assert exc.value.category.value == "slice_out_of_range"


def test_empty_window_slice_is_zero_copy_and_valid():
    view = import_raw(descriptor_to_raw(fixed_descriptor("int32", [1, 2]))).view
    sub = view.slice(2, 0)
    assert sub.to_pylist() == []
    assert sub.logical_null_count() == 0
    assert all(sub.shares_memory_with(view).values())


# --------------------------------------------------------------- ownership
def test_view_remains_valid_after_source_objects_released():
    desc = fixed_descriptor("int64", [101, 202, None, 404, 505])
    raw = descriptor_to_raw(desc)  # holds the python bytes
    view_holder = {}

    def build_and_release():
        # The source buffers are plain locals; after this function returns and
        # GC runs the only thing keeping the memory alive may be the view's
        # own owner chain.
        local_raw = RawColumnBuffers(
            type_name=raw.type_name, length=raw.length,
            validity=bytearray(raw.validity) if raw.validity else None,
            offsets=None,
            data=bytearray(raw.data),
            null_count=raw.null_count,
        )
        result = import_raw(local_raw)
        view_holder["view"] = result.view
        del local_raw

    build_and_release()
    gc.collect()
    view: ColumnView = view_holder["view"]
    # Local source objects are out of scope and GC has run. Whether or not
    # Arrow internally retains the exporter, the view's own owner chain must
    # keep the physical memory readable (no dangling access).
    assert view.to_pylist() == [101, 202, None, 404, 505]
    assert view.slice(1, 3).to_pylist() == [202, None, 404]
    assert view.buffer_identities()[0].size > 0  # validity still addressable


def test_string_view_remains_valid_after_source_released():
    desc = string_descriptor(["first", None, "", "fourth-value"])
    raw = descriptor_to_raw(desc)
    view_holder = {}

    def build():
        local = RawColumnBuffers(raw.type_name, raw.length,
                                 bytes(raw.validity) if raw.validity else None,
                                 bytes(raw.offsets), bytes(raw.data), raw.null_count)
        result = import_raw(local)
        view_holder["owners_alive"] = bool(result.view._owners)
        view_holder["view"] = result.view

    build()
    gc.collect()
    v = view_holder["view"]
    assert view_holder["owners_alive"]
    assert v.to_pylist() == ["first", None, "", "fourth-value"]
    assert v.slice(1, 2).to_pylist() == [None, ""]


def test_view_construction_honors_preexisting_array_offset():
    # An imported array can already carry an array-level offset (IPC chunks,
    # concatenate internals). buffers() then returns parent buffers, so the
    # view must adopt array.offset rather than assume 0.
    arr = pa.array([10, 11, 12, 13, 14, 15], pa.int32()).slice(3, 3)
    view = ColumnView.from_array(arr)
    assert view.offset == 3 and view.length == 3
    assert view.to_pylist() == [13, 14, 15]
    sub = view.slice(1, 1)
    assert sub.offset == 4 and sub.to_pylist() == [14]
    assert all(sub.shares_memory_with(view).values())


def test_zero_copy_slice_proof_via_pointer_values():
    values = [i for i in range(20)]
    view = import_raw(descriptor_to_raw(fixed_descriptor("uint32", values))).view
    sub = view.slice(11, 5)
    sharing = sub.shares_memory_with(view)
    assert sharing == {"data": True}  # no validity buffer in this fixture
    assert sub.data_buffer.address == view.data_buffer.address
    # Physical read starts at offset 11 even though the buffer base is shared.
    width = 4
    assert struct.unpack("<I", bytes(sub.data_buffer[11 * width:11 * width + 4]))[0] == 11
