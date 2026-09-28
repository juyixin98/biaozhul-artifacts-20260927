"""Unit tests for the concat kernel: casts, NULL handling, copy accounting."""

from __future__ import annotations

import pyarrow as pa
import pytest

from arrowzero.adapters import import_pylist
from arrowzero.kernel.concat import CastError, concat
from helpers import pyarrow_values

pytestmark = pytest.mark.unit


def test_concat_primitives_matches_pyarrow_and_copies():
    a, _ = import_pylist([1, 2, None, 4], "int32")
    b, _ = import_pylist([5, None, 7], "int32")
    out, ledger = concat([a.slice(1, 2), b.slice(0, 2)])
    assert out.to_pylist() == [2, None, 5, None]
    assert pyarrow_values(out) == [2, None, 5, None]
    # reference: pyarrow's own concat over the same inputs
    ref = pa.concat_arrays(
        [a.slice(1, 2).to_arrow(), b.slice(0, 2).to_arrow()]
    ).to_pylist()
    assert out.to_pylist() == ref
    # 4 x int32 data bytes allocated/copied; validity present (1 byte)
    bufmap = {b["name"]: b for b in ledger.as_dict()["output_buffers"]}
    assert bufmap["data"]["size"] == 16
    assert bufmap["data"]["aliased_source"] is None
    assert ledger.copied_bytes == 17  # 16 data + 1 validity
    assert ledger.allocated_bytes == 17


def test_concat_no_null_allocates_no_validity():
    a, _ = import_pylist([1, 2], "int64")
    b, _ = import_pylist([3, 4], "int64")
    out, ledger = concat([a, b])
    assert out.to_pylist() == [1, 2, 3, 4]
    bufmap = {b["name"]: b for b in ledger.as_dict()["output_buffers"]}
    assert bufmap["validity"]["size"] == 0
    assert ledger.copied_bytes == 32  # 4 x 8, validity None -> not copied


def test_cross_type_concat_without_cast_is_type_mismatch():
    a, _ = import_pylist([1, 2], "int32")
    b, _ = import_pylist([3, 4], "int64")
    with pytest.raises(CastError) as exc:
        concat([a, b])
    assert "identically typed" in str(exc.value) or "differing types" in str(exc.value)


def test_cross_type_concat_with_explicit_cast_matches_pyarrow():
    a, _ = import_pylist([1, 2], "int32")
    b, _ = import_pylist([3, 4], "int64")
    out, ledger = concat([a, b], cast_to="int64")
    assert str(out.type) == "int64"
    assert out.to_pylist() == [1, 2, 3, 4]
    assert pyarrow_values(out) == pa.concat_arrays(
        [a.to_arrow().cast(pa.int64()), b.to_arrow()]
    ).to_pylist()


def test_explicit_cast_to_narrower_rejected_when_lossy():
    a, _ = import_pylist([70000], "int64")
    b, _ = import_pylist([1], "int16")
    with pytest.raises(CastError, match="unsafe cast"):
        concat([a, b], cast_to="int16")
    # string <-> numeric conversions are not offered by this kernel at all
    s, _ = import_pylist(["x"], "utf8")
    with pytest.raises(CastError):
        concat([a, s], cast_to="int64")


def test_explicit_cast_widening_keeps_values():
    a, _ = import_pylist([1, 2], "int16")
    b, _ = import_pylist([3, 4], "int32")
    out, _ = concat([a, b], cast_to="int64")
    assert out.to_pylist() == [1, 2, 3, 4]


def test_concat_strings_with_empty_and_null_slots():
    a, _ = import_pylist(["a", None, "", "bc"], "utf8")
    b, _ = import_pylist(["", None, "d"], "utf8")
    out, ledger = concat([a.slice(1, 3), b])
    expected = [None, "", "bc", "", None, "d"]
    assert out.to_pylist() == expected
    assert pyarrow_values(out) == expected
    ref = pa.concat_arrays(
        [a.slice(1, 3).to_arrow(), b.to_arrow()]
    ).to_pylist()
    assert out.to_pylist() == ref
    # offsets: 6 elements -> 7 x int32 = 28 bytes; data: bc + d = 3 bytes;
    # validity present = 1 byte.
    bufmap = {b_["name"]: b_ for b_ in ledger.as_dict()["output_buffers"]}
    assert bufmap["offsets"]["size"] == 28
    assert bufmap["data"]["size"] == 3
    assert ledger.copied_bytes == 32
    # output passes Arrow's own full validation
    out.to_arrow().validate(full=True)


def test_concat_multibyte_utf8_preserved():
    a, _ = import_pylist(["βγ", None], "utf8")
    b, _ = import_pylist(["δεζ"], "utf8")
    out, _ = concat([a, b])
    assert out.to_pylist() == ["βγ", None, "δεζ"]
    assert out.to_arrow().to_pylist() == ["βγ", None, "δεζ"]


def test_concat_empty_inputs_error_not_success():
    with pytest.raises(ValueError):
        concat([])


def test_concat_output_buffers_do_not_alias_sources():
    a, _ = import_pylist([1, 2, 3, 4], "int32")
    sl = a.slice(1, 3)
    out, ledger = concat([sl])
    # materialized: fresh allocation even though there is only one chunk
    for buf in ledger.as_dict()["output_buffers"]:
        if buf["size"]:
            assert buf["aliased_source"] is None
    assert out.to_pylist() == [2, 3, 4]
