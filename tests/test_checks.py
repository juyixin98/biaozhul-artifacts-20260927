"""Unit tests for the independent validator: validity / offsets / data layers."""

from __future__ import annotations

import base64
import struct

import pyarrow as pa
import pytest

from arrowzero.kernel.checks import (
    ValidationError,
    ViolationCode,
    ensure_valid,
    validate_buffers,
)

pytestmark = pytest.mark.unit


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def codes(descriptor: dict) -> list[str]:
    t = pa.utf8() if descriptor["type"] in ("utf8", "string") else getattr(
        pa, descriptor["type"]
    )()
    raw = [None if x is None else base64.b64decode(x) for x in descriptor["buffers"]]
    return [
        v.code.value
        for v in validate_buffers(
            t,
            descriptor["length"],
            raw,
            logical_offset=descriptor.get("offset", 0),
        )
    ]


def test_decreasing_offsets_classified_as_offset_layer():
    desc = {
        "type": "utf8", "length": 3,
        "buffers": [None, b64(struct.pack("<iiii", 0, 3, 2, 5)), b64(b"abcde")],
    }
    violations = {
        (v.code.value, v.layer)
        for v in validate_buffers(
            pa.utf8(), 3,
            [None, struct.pack("<iiii", 0, 3, 2, 5), b"abcde"],
        )
    }
    assert violations == {("DECREASING_OFFSET", "offsets")}


def test_first_offset_must_be_zero_at_array_start():
    violations = validate_buffers(
        pa.utf8(), 2,
        [None, struct.pack("<iii", 4, 5, 6), b"abcdef"],
    )
    assert ("INVALID_FIRST_OFFSET", "offsets") in {(v.code.value, v.layer) for v in violations}


def test_first_offset_may_be_nonzero_with_logical_offset():
    # View at offset 1: first *visible* int32 is offsets[1]; offsets[0] is
    # inherited producer state and no INVALID_FIRST_OFFSET is emitted.
    violations = validate_buffers(
        pa.utf8(), 2,
        [None, struct.pack("<iiii", 0, 1, 1, 2), b"ab"],
        logical_offset=1,
    )
    assert violations == []


def test_out_of_bounds_final_offset_uses_data_layer():
    violations = validate_buffers(
        pa.utf8(), 2,
        [None, struct.pack("<iii", 0, 3, 10), b"abcde"],
    )
    layers = {(v.code.value, v.layer) for v in violations}
    assert ("OFFSET_OUT_OF_BOUNDS", "data") in layers


def test_data_buffer_short_for_primitive():
    violations = validate_buffers(
        pa.int32(), 6, [None, b"\x00" * 20]
    )
    assert violations[0].code is ViolationCode.BUFFER_TOO_SHORT
    assert violations[0].layer == "data"


def test_validity_buffer_too_short():
    violations = validate_buffers(
        pa.int32(), 20, [b"\x00", b"\x00" * 80]
    )
    assert (violations[0].code.value, violations[0].layer) == (
        "BUFFER_TOO_SHORT", "validity"
    )


def test_padding_bits_in_validity():
    violations = validate_buffers(
        pa.int32(), 6, [bytes([0xFF]), b"\x00" * 24]
    )
    assert any(v.code is ViolationCode.INVALID_PADDING for v in violations)


def test_invalid_utf8_payload_detected_for_present_slots_only():
    # slot 0 valid with 0xFF; slot 1 NULL whose span would also be invalid
    validity = bytes([0b00000001])
    violations = validate_buffers(
        pa.utf8(), 2,
        [validity, struct.pack("<iii", 0, 1, 2), b"\xff\xff"],
    )
    utf8 = [v for v in violations if v.code is ViolationCode.UTF8_INVALID]
    assert len(utf8) == 1
    assert utf8[0].index == 0


def test_multiple_violations_are_all_returned():
    # decreasing offsets AND data too short AND garbage padding -> all reported
    violations = validate_buffers(
        pa.utf8(), 3,
        [bytes([0xFF]), struct.pack("<iiii", 0, 9, 8, 20), b"abc"],
    )
    found = {v.code for v in violations}
    assert ViolationCode.INVALID_PADDING in found
    assert ViolationCode.DECREASING_OFFSET in found
    assert ViolationCode.OFFSET_OUT_OF_BOUNDS in found


def test_missing_data_buffer():
    violations = validate_buffers(pa.int32(), 1, [None, None])
    assert (violations[0].code.value, violations[0].layer) == (
        "MISSING_BUFFER", "data"
    )


def test_unsupported_type_rejected():
    violations = validate_buffers(pa.bool_(), 1, [None, b"\x01"])
    assert violations[0].code is ViolationCode.TYPE_UNSUPPORTED


def test_buffer_count_mismatch():
    violations = validate_buffers(pa.int32(), 1, [None])
    assert violations[0].code is ViolationCode.BUFFER_LAYOUT


def test_ensure_valid_raises_with_dicts():
    with pytest.raises(ValidationError) as exc:
        ensure_valid(pa.int32(), 6, [None, b"\x00" * 4])
    dicts = exc.value.to_dicts()
    assert dicts[0]["code"] == "BUFFER_TOO_SHORT"
    assert dicts[0]["layer"] == "data"
    assert str(exc.value)  # message is human readable


def test_fixture_expectation_codes():
    import fixtures

    for name, descriptor in fixtures.MALFORMED_DESCRIPTORS.items():
        if name == "nonzero_offset_view":
            assert codes(descriptor) == []
        else:
            for expected in descriptor["expect_violations"]:
                assert expected in codes(descriptor), name
