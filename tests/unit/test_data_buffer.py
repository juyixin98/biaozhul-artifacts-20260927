"""Unit tests: data-buffer length/alignment checks, independently per family."""
from __future__ import annotations

import math

import pytest

from app.adapters.descriptor import descriptor_to_raw
from app.validation.checks import validate
from tests.fixtures.oracle import expected_fixed, fixed_descriptor

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("type_name,values", [
    ("int8", [-128, -1, 0, 127]),
    ("uint8", [0, 1, 255]),
    ("int16", [-32768, 0, 32767]),
    ("uint16", [0, 65535]),
    ("int32", [-2**31, 0, 2**31 - 1]),
    ("uint32", [0, 2**32 - 1]),
    ("int64", [-2**63, 0, 2**63 - 1]),
    ("uint64", [0, 2**64 - 1]),
    ("float32", [1.5, -2.25]),
    ("float64", [math.pi, -math.e]),
])
def test_fixed_types_data_buffer_exact_length_and_values(type_name, values):
    desc = fixed_descriptor(type_name, values)
    report = validate(descriptor_to_raw(desc))
    assert report.ok, report.failure_categories
    assert report.values == expected_fixed(type_name, values)
    size_check = next(c for c in report.checks if c.name == "data_buffer_covers_elements")
    assert size_check.evidence["trailing_bytes"] == 0


def test_data_buffer_short_by_two_bytes_detected():
    desc = fixed_descriptor("int32", [1, 2, 3, 4], drop_data_bytes=2)
    report = validate(descriptor_to_raw(desc))
    assert "data_too_short" in report.failure_categories
    check = next(c for c in report.failures if c.category == "data_too_short")
    assert check.evidence == {"required_bytes": 16, "received_bytes": 14, "missing_bytes": 2}


def test_data_buffer_trailing_bytes_reported_but_valid():
    # Trailing bytes must be a whole-element multiple to remain aligned.
    desc = fixed_descriptor("int32", [1, 2], extra_data=4)
    report = validate(descriptor_to_raw(desc))
    assert report.ok, report.failure_categories
    check = next(c for c in report.checks if c.name == "data_buffer_covers_elements")
    assert check.evidence["trailing_bytes"] == 4


def test_data_buffer_unaligned_trailing_bytes_flagged():
    desc = fixed_descriptor("int32", [1, 2], extra_data=3)
    report = validate(descriptor_to_raw(desc))
    assert "buffer_not_aligned" in report.failure_categories


def test_unsupported_type_rejected_at_schema_group():
    desc = {"type": "bool", "length": 1, "data": "AA=="}
    report = validate(descriptor_to_raw(desc))
    assert not report.ok
    assert report.failure_categories == ["unsupported_type"]
    # Unsupported types must skip the fixed/string specific checks cleanly.
    assert all(c.ok for c in report.checks if c.group != "schema")


def test_malformed_length_rejected_at_adapter():
    from app.errors import LayoutError
    with pytest.raises(LayoutError) as exc:
        descriptor_to_raw({"type": "int32", "length": -1, "data": ""})
    assert exc.value.category.value == "malformed_payload"


def test_non_utf8_data_byte_is_semantic_failure_not_success():
    # Build offsets/data by hand: one value pointing at invalid utf8 bytes.
    import base64
    import struct
    desc = {
        "type": "utf8",
        "length": 1,
        "data": base64.b64encode(b"\xff\xfe").decode(),
        "offsets": base64.b64encode(struct.pack("<2i", 0, 2)).decode(),
    }
    report = validate(descriptor_to_raw(desc))
    assert "semantic_scan_failed" in report.failure_categories
    check = next(c for c in report.failures if c.category == "semantic_scan_failed")
    assert check.evidence["index"] == 0
