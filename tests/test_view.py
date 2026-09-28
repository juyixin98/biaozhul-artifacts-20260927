"""Unit tests for ColumnView: non-zero-offset slicing, NULL semantics, ownership."""

from __future__ import annotations

import gc

import pyarrow as pa
import pytest

from arrowzero.adapters import import_ipc_stream, import_pylist
from arrowzero.kernel.view import ColumnView
from helpers import (
    buffers_identical,
    oracle_null_map,
    oracle_primitive_values,
    oracle_string_values,
    pyarrow_values,
)

pytestmark = pytest.mark.unit


def test_nonzero_offset_null_and_index_primitive():
    # NULLs at 4 and 7; slice starting at 6 crosses the byte boundary.
    view, _ = import_pylist([0, 1, 2, 3, None, 5, 6, None, 8, 9], "int32")
    sl = view.slice(6, 4)
    assert sl.offset == 6
    # NULL judgment with non-zero offset: index 1 of the slice == global 7
    assert [sl.is_null(i) for i in range(4)] == [False, True, False, False]
    assert sl.count_nulls() == 1
    assert sl.to_pylist() == [6, None, 8, 9]
    # independent oracles: raw-buffer oracle and PyArrow agree with the kernel
    validity, data = view.buffers[0], view.buffers[1]
    assert [sl.is_null(i) for i in range(4)] == oracle_null_map(
        bytes(validity), 4, offset=6
    )
    assert [sl.get(i) for i in range(4) if not sl.is_null(i)] == [
        x for x, n in zip(
            oracle_primitive_values(bytes(data), "int32", 4, 6),
            oracle_null_map(bytes(validity), 4, 6),
        ) if not n
    ]
    assert pyarrow_values(sl) == [6, None, 8, 9]


def test_nonzero_offset_strings_empty_string_is_not_null():
    view, _ = import_pylist(
        ["alpha", None, "", "βγ", "", None, "z", "δεζ"], "utf8"
    )
    sl = view.slice(1, 5)
    assert sl.offset == 1
    assert [sl.is_null(i) for i in range(5)] == [True, False, False, False, True]
    # "" and NULL must be distinguishable at non-zero offset
    assert sl.to_pylist() == [None, "", "βγ", "", None]
    validity, offs, data = (bytes(b) for b in view.buffers)
    assert sl.to_pylist() == oracle_string_values(validity, offs, data, 5, 1)
    assert pyarrow_values(sl) == oracle_string_values(validity, offs, data, 5, 1)


def test_slice_shares_every_buffer_zero_copy():
    view, _ = import_pylist(["a", None, "", "bc"], "utf8")
    sl = view.slice(1, 3)
    identity = buffers_identical(view, sl)
    assert identity == {"validity": True, "offsets": True, "data": True}
    assert sl.offset == view.offset + 1


def test_slice_range_errors_are_index_errors():
    view, _ = import_pylist([1, 2, 3], "int32")
    with pytest.raises(IndexError):
        view.slice(2, 5)
    with pytest.raises(IndexError):
        view.slice(-1)
    empty = view.slice(3, 0)
    assert empty.to_pylist() == []
    assert empty.count_nulls() == 0


def test_bitmap_byte_boundary_nulls_every_position():
    # One NULL at every index 0..15 across several separate slices, including
    # the byte boundaries 7 and 8.
    values = [i for i in range(16)]
    for null_index in range(16):
        vals = values.copy()
        vals[null_index] = None
        view, _ = import_pylist(vals, "int64")
        for start in (0, 1, 6, 7, 8, 9, 15):
            if start >= 16:
                continue
            sl = view.slice(start, 16 - start)
            expected_null = null_index >= start
            if expected_null:
                assert sl.is_null(null_index - start) is True
            else:
                assert sl.count_nulls() == 0
            assert sl.count_nulls() == (1 if expected_null else 0)
            assert pyarrow_values(sl) == sl.to_pylist()

def test_view_pins_buffers_after_source_array_release():
    src = pa.array(["keep", None, "alive", "", "x"])
    src_spans = [(b.address, b.size) if b is not None else None for b in src.buffers()]
    view = ColumnView.from_array(src, origin="array")
    sl = view.slice(1, 3)
    del src
    gc.collect()
    # The view must not read dangling memory.
    assert sl.to_pylist() == [None, "alive", ""]
    assert sl.count_nulls() == 1
    # The underlying allocations (address+size) survive the source wrapper.
    sl_spans = [(b.address, b.size) if b is not None else None for b in sl.buffers]
    assert sl_spans == src_spans


def test_ipc_view_reads_correctly_after_payload_and_reader_released():
    from arrowzero.adapters import export_ipc_stream

    original = ["alpha", None, "", "βγ", "", None, "z", "δεζ"]
    view, evidence = import_pylist(original, "utf8")
    payload = export_ipc_stream(view)
    imported, ipc_evidence = import_ipc_stream(payload)
    assert ipc_evidence["zero_copy"] is True
    addresses_before = [
        None if b is None else b.address for b in imported.buffers
    ]
    sl = imported.slice(1, 5)
    del payload
    del view
    gc.collect()
    # Even though the IPC payload bytes object is gone, _owners pins the
    # underlying allocation the buffers live in.
    assert sl.to_pylist() == [None, "", "βγ", "", None]
    assert pyarrow_values(sl) == [None, "", "βγ", "", None]
    assert [None if b is None else b.address for b in sl.buffers] == addresses_before


def test_describe_reports_geometry():
    view, _ = import_pylist([1, None, 3], "int16")
    sl = view.slice(1, 2)
    d = sl.describe()
    assert d == {
        "type": "int16",
        "length": 2,
        "offset": 1,
        "null_count": 1,
        "origin": "slice@pylist",
        "buffers": d["buffers"],
    }
    assert {b["name"] for b in d["buffers"]} == {"validity", "data"}
