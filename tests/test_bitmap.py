"""Unit tests for validity bitmap primitives (byte boundaries)."""

from __future__ import annotations

import pytest

from arrowzero.kernel.bitmap import (
    build_validity,
    count_padding_errors,
    count_set_bits,
    is_bit_set,
)

pytestmark = pytest.mark.unit


def test_bit_lsb_order_within_bytes():
    # bits: 0 and 2 set in byte 0; bit 1 set in byte 1 (index 9)
    buf = bytes([0b00000101, 0b00000010])
    assert [is_bit_set(buf, i) for i in range(10)] == [
        True, False, True, False, False, False, False, False, False, True
    ]


def test_byte_boundary_set_count():
    # set bits at indices 0,7,8,15,16 -> across three byte boundaries
    buf = bytearray(3)
    for i in (0, 7, 8, 15, 16):
        buf[i >> 3] |= 1 << (i & 7)
    assert count_set_bits(buf, length=17) == 5
    # windowed counts with non-zero offset
    assert count_set_bits(buf, length=8, offset=1) == 2  # bits 7,8
    assert count_set_bits(buf, length=3, offset=14) == 2  # bits 15,16
    assert count_set_bits(buf, length=2, offset=14) == 1  # only bit 15


def test_padding_bits_detection():
    # length 6 -> bits 6,7 must be zero
    assert count_padding_errors(bytes([0b00111111]), 6) == []
    assert count_padding_errors(bytes([0b10111111]), 6) == [7]
    assert sorted(count_padding_errors(bytes([0xFF]), 6)) == [6, 7]
    # aligned and empty have no padding bits
    assert count_padding_errors(bytes([0xFF]), 8) == []
    assert count_padding_errors(b"", 0) == []


def test_build_validity_none_when_all_present():
    assert build_validity([True, True]) is None
    out = build_validity([True, False, True, False, True, False, True, False, True])
    assert out is not None
    assert count_set_bits(out, 9) == 5
    assert count_padding_errors(out, 9) == []  # tail bits zeroed
