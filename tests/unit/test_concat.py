"""Unit tests: concat kernel copy accounting, validity merge, cast rule."""
from __future__ import annotations

import pyarrow as pa
import pytest

from app.adapters.descriptor import descriptor_to_raw
from app.adapters.importer import import_raw
from app.core.concat import concat
from app.errors import LayoutError
from tests.fixtures.oracle import fixed_descriptor, string_descriptor

pytestmark = pytest.mark.unit


def _view(desc):
    return import_raw(descriptor_to_raw(desc)).view


def test_primitive_concat_copies_exact_data_volume_and_values():
    a = _view(fixed_descriptor("int32", [1, 2, 3]))
    b = _view(fixed_descriptor("int32", [4, 5]))
    result = concat([a, b])
    assert result.report.zero_copy is False
    assert result.view.to_pylist() == [1, 2, 3, 4, 5]
    # 5 x int32 data bytes; no validity bitmap anywhere.
    assert result.report.bytes_data == 20
    assert result.report.bytes_validity == 0
    assert result.report.total_bytes == 20
    # Result memory is freshly allocated (address differs from both inputs).
    assert result.view.data_buffer.address not in {a.data_buffer.address, b.data_buffer.address}
    # PyArrow agreement.
    assert result.view.materialize().to_pylist() == pa.concat_arrays([
        pa.array([1, 2, 3], pa.int32()), pa.array([4, 5], pa.int32())]).to_pylist()


def test_concat_with_nulls_merges_bitmaps_logically_and_counts_bytes():
    a = _view(fixed_descriptor("int64", [1, None, 3]))
    b = _view(fixed_descriptor("int64", [None, 5]))
    result = concat([a, b])
    assert result.view.to_pylist() == [1, None, 3, None, 5]
    assert [result.view.is_null(i) for i in range(5)] == [False, True, False, True, False]
    assert result.report.bytes_validity == 1      # ceil(5/8)
    assert result.report.bytes_data == 5 * 8
    assert result.view.materialize().to_pylist() == pa.concat_arrays([
        pa.array([1, None, 3], pa.int64()), pa.array([None, 5], pa.int64())]).to_pylist()


def test_concat_of_sliced_views_copies_only_logical_windows():
    a = _view(fixed_descriptor("int32", list(range(10))))
    b = _view(fixed_descriptor("int32", list(range(100, 110))))
    sa, sb = a.slice(7, 3), b.slice(2, 4)       # 3 + 4 elements
    result = concat([sa, sb])
    assert result.view.to_pylist() == [7, 8, 9, 102, 103, 104, 105]
    assert result.report.bytes_data == 7 * 4
    # Both input data buffers were read at non-zero physical offsets.
    data_steps = [s for s in result.report.steps if s.startswith("data[")]
    assert "data[0]: copied physical bytes [28,40) = 12 bytes" in data_steps[0]
    assert "data[1]: copied physical bytes [8,24) = 16 bytes" in data_steps[1]


def test_concat_strings_rebuilds_offsets_and_data_with_exact_counts():
    a = _view(string_descriptor(["ab", None, "cdef"]))
    b = _view(string_descriptor(["", "ghij"]))
    result = concat([a, b])
    assert result.view.to_pylist() == ["ab", None, "cdef", "", "ghij"]
    assert [result.view.is_null(i) for i in range(5)] == [False, True, False, False, False]
    assert result.report.bytes_offsets == 6 * 4
    assert result.report.bytes_data == 2 + 4 + 0 + 4
    assert result.report.bytes_validity == 1
    assert result.view.materialize().to_pylist() == pa.concat_arrays([
        pa.array(["ab", None, "cdef"], pa.utf8()), pa.array(["", "ghij"], pa.utf8())]).to_pylist()


def test_cross_type_concat_without_target_type_is_type_mismatch():
    a = _view(fixed_descriptor("int32", [1, 2]))
    b = _view(fixed_descriptor("int64", [3, 4]))
    with pytest.raises(LayoutError) as exc:
        concat([a, b])
    assert exc.value.category.value == "type_mismatch"
    assert set(exc.value.detail["types"]) == {"int32", "int64"}


def test_cross_type_concat_requires_explicit_target_and_counts_cast():
    a = _view(fixed_descriptor("int32", [1, 2]))
    b = _view(fixed_descriptor("int64", [3, 4]))
    result = concat([a, b], target_type="int64")
    assert result.view.type_name == "int64"
    assert result.view.to_pylist() == [1, 2, 3, 4]
    assert result.report.cast_target == "int64"
    # First segment cast (2 x int64 = 16 data bytes), second already int64;
    # concat then copies all 4 elements (32 data bytes).
    assert result.report.bytes_cast == 16
    assert result.report.bytes_data == 32
    assert result.view.materialize().to_pylist() == pa.array([1, 2, 3, 4], pa.int64()).to_pylist()


def test_cast_of_sliced_segment_counts_only_logical_window():
    # Parent int32 has 8 elements (32 data bytes); cast only a 2-element slice.
    big = _view(fixed_descriptor("int32", list(range(8))))
    sub = big.slice(5, 2)                       # [5, 6]
    other = _view(fixed_descriptor("int64", [7, 8]))
    result = concat([sub, other], target_type="int64")
    assert result.view.to_pylist() == [5, 6, 7, 8]
    # Cast produced 2 x int64 = 16 bytes, NOT the parent's full 32 bytes.
    assert result.report.bytes_cast == 16
    assert result.report.bytes_data == 32


def test_cross_type_concat_cast_to_unsupported_target_rejected():
    a = _view(fixed_descriptor("int32", [1]))
    with pytest.raises(LayoutError) as exc:
        concat([a, a], target_type="bool")
    assert exc.value.category.value == "unsupported_type"


def test_single_input_concat_is_passthrough_zero_copy():
    a = _view(fixed_descriptor("int32", [1, 2, 3]))
    result = concat([a])
    assert result.report.zero_copy is True
    assert result.report.total_bytes == 0
    assert result.view is a


def test_empty_concat_is_categorized():
    with pytest.raises(LayoutError) as exc:
        concat([])
    assert exc.value.category.value == "malformed_payload"
