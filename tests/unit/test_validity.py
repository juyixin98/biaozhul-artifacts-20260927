"""Unit tests: validity bitmap checks, including byte-boundary spans.

The reviewer explicitly checks that bitmap bytes are validated independently
and that byte boundaries (multiples of 8) are handled. Expected bitmaps here
are hand-packed, not produced by PyArrow.
"""
from __future__ import annotations

import pytest

from app.adapters.descriptor import descriptor_to_raw
from app.core.bitmath import bit_get, count_set_bits, pack_validity, trailing_padding_bits_are_zero
from app.validation.checks import validate
from tests.fixtures.oracle import fixed_descriptor

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("n", list(range(0, 18)))
def test_packed_bitmap_roundtrip_every_length(n):
    # Alternate valid/invalid; force NULL at every third slot.
    flags = [(i % 3) != 0 for i in range(n)]
    packed = pack_validity(flags)
    assert len(packed) == (n + 7) // 8
    for i in range(n):
        assert bit_get(packed, i) == (1 if flags[i] else 0), i
    assert count_set_bits(packed, 0, n) == sum(flags)
    assert trailing_padding_bits_are_zero(packed, n)


def test_bitmap_byte_boundary_nulls_every_8th_slot():
    # 16 elements with NULLs exactly at positions 7 and 15 (last bit of bytes).
    values = [i for i in range(16)]
    values[7] = None
    values[15] = None
    desc = fixed_descriptor("int32", values)
    report = validate(descriptor_to_raw(desc))
    assert report.ok, report.failure_categories
    assert report.computed_null_count == 2
    # Direct bitmap evidence.
    vb = descriptor_to_raw(desc).validity
    assert bit_get(vb, 7) == 0 and bit_get(vb, 15) == 0
    assert bit_get(vb, 6) == 1 and bit_get(vb, 8) == 1
    assert count_set_bits(vb, 0, 16) == 14


def test_bitmap_non_byte_aligned_length_9_padding_ok_then_bad():
    values = [1 if i != 4 else None for i in range(9)]
    desc = fixed_descriptor("int32", values)
    report = validate(descriptor_to_raw(desc))
    assert report.ok
    assert report.computed_null_count == 1

    bad = fixed_descriptor("int32", values, trailing_bits=True)
    report = validate(descriptor_to_raw(bad))
    cats = report.failure_categories
    assert "validity_trailing_bits_set" in cats
    check = next(c for c in report.failures if c.category == "validity_trailing_bits_set")
    # Evidence must point at the exact byte.
    assert check.evidence["logical_span"] == 9
    assert check.evidence["last_byte"] & 0x80


def test_validity_buffer_one_byte_short_detected():
    # 10 elements need 2 bitmap bytes; supply only 1.
    import base64
    values = [None] * 10
    desc = fixed_descriptor("int64", values)
    raw = base64.b64decode(desc["validity"])
    desc["validity"] = base64.b64encode(raw[:-1]).decode()
    report = validate(descriptor_to_raw(desc))
    assert "validity_too_short" in report.failure_categories
    check = next(c for c in report.failures if c.category == "validity_too_short")
    assert check.evidence["required_bytes_for_logical_span"] == 2
    assert check.evidence["received_bytes"] == 1


def test_claimed_null_count_wrong_is_its_own_failure_category():
    values = [1, None, 3, None]
    desc = fixed_descriptor("int32", values, null_count=1)  # bitmap actually has 2 nulls
    report = validate(descriptor_to_raw(desc))
    assert "null_count_mismatch" in report.failure_categories
    check = next(c for c in report.failures if c.category == "null_count_mismatch")
    assert check.evidence == {"claimed": 1, "computed": 2, "length": 4}


def test_omitted_validity_means_all_valid_even_with_claim():
    desc = fixed_descriptor("int32", [1, 2, 3])
    report = validate(descriptor_to_raw(desc))
    assert report.ok and report.computed_null_count == 0
