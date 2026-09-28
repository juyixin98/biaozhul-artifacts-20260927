"""Unit tests: int32 offset checks for utf8 columns, independently assessed."""
from __future__ import annotations

import base64
import struct

import pytest

from app.adapters.descriptor import descriptor_to_raw
from app.validation.checks import validate
from tests.fixtures.oracle import string_descriptor

pytestmark = pytest.mark.unit


def test_empty_string_is_valid_zero_span_not_null():
    desc = string_descriptor(["alpha", "", None, "", "zz"])
    report = validate(descriptor_to_raw(desc))
    assert report.ok, report.failure_categories
    sem = next(c for c in report.checks if c.name == "all_non_null_strings_decode_as_utf8")
    assert sem.evidence["empty_strings"] == 2
    assert sem.evidence["nulls"] == 1
    assert report.values == ["alpha", "", None, "", "zz"]
    assert report.computed_null_count == 1


def test_decreasing_offsets_detected_with_index_and_values():
    def mutate(offsets, data):
        # 3 values; force offset[2] below offset[1].
        offsets[2] = offsets[1] - 2

    desc = string_descriptor(["abc", "de", "f"], mutate=mutate)
    report = validate(descriptor_to_raw(desc))
    assert "offsets_not_monotonic" in report.failure_categories
    check = next(c for c in report.failures if c.category == "offsets_not_monotonic")
    assert check.evidence["index"] == 2
    assert check.evidence["value"] == check.evidence["previous"] - 2
    # A non-monotonic layout must never be presented as valid.
    assert report.ok is False


def test_negative_offset_is_out_of_range_category():
    def mutate(offsets, data):
        offsets[1] = -1
        # keep monotonic-ish ordering so we isolate the negative check
        offsets[0] = -1

    desc = string_descriptor(["a", "b"], mutate=mutate)
    report = validate(descriptor_to_raw(desc))
    assert "offset_not_zero" in report.failure_categories
    assert "offset_out_of_range" in report.failure_categories


def test_final_offset_beyond_data_is_out_of_range():
    def mutate(offsets, data):
        offsets[-1] = len(data) + 10

    desc = string_descriptor(["a", "bb"], mutate=mutate)
    report = validate(descriptor_to_raw(desc))
    assert "offset_out_of_range" in report.failure_categories
    check = next(c for c in report.failures if c.category == "offset_out_of_range")
    assert check.evidence["final_offset"] == check.evidence["data_size"] + 10


def test_offsets_buffer_too_short_for_element_count():
    desc = string_descriptor(["a", "bb", "ccc"])
    raw = base64.b64decode(desc["offsets"])
    desc["offsets"] = base64.b64encode(raw[:-4]).decode()  # drop last int32
    report = validate(descriptor_to_raw(desc))
    assert "offsets_too_short" in report.failure_categories
    check = next(c for c in report.failures if c.category == "offsets_too_short")
    assert check.evidence["required_bytes"] == 16
    assert check.evidence["received_bytes"] == 12


def test_offsets_unaligned_bytes_flagged():
    # 2 elements -> 3 offsets -> 12 bytes; append two bytes to hit 14 % 4 == 2.
    desc = string_descriptor(["a", "bb"])
    raw = base64.b64decode(desc["offsets"]) + b"\x00\x00"
    desc["offsets"] = base64.b64encode(raw).decode()
    report = validate(descriptor_to_raw(desc))
    assert "buffer_not_aligned" in report.failure_categories
    check = next(c for c in report.failures if c.category == "buffer_not_aligned")
    assert check.evidence["remainder"] == 2


def test_offsets_with_aligned_trailing_bytes_are_accepted():
    desc = string_descriptor(["a", "bb"])
    raw = base64.b64decode(desc["offsets"]) + b"\x00\x00\x00\x00"
    desc["offsets"] = base64.b64encode(raw).decode()
    report = validate(descriptor_to_raw(desc))
    assert report.ok, report.failure_categories
    size_check = next(c for c in report.checks if c.name == "offsets_length_covers_elements")
    assert size_check.evidence["trailing_bytes"] == 4


def test_first_offset_nonzero_rejected():
    def mutate(offsets, data):
        # Shift all offsets up by 5, preserving monotonicity, and pad data.
        for i in range(len(offsets)):
            offsets[i] += 5
        data.extend(b"xxxxx")

    desc = string_descriptor(["a"], mutate=mutate)
    report = validate(descriptor_to_raw(desc))
    assert "offset_not_zero" in report.failure_categories


def test_empty_string_column_requires_zero_offset_entry():
    desc = string_descriptor([])
    report = validate(descriptor_to_raw(desc))
    assert report.ok
    raw = descriptor_to_raw(desc)
    assert raw.offsets == struct.pack("<i", 0)
    assert raw.data == b""
