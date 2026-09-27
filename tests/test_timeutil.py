"""Tests for the strict time kernel — concrete accepted/rejected tokens."""
from __future__ import annotations

import pytest

from app.core.timeutil import TimeParseError, format_ms, parse_ms


@pytest.mark.parametrize(
    "token,fmt,expected",
    [
        ("00:00:00,000", "srt", 0),
        ("00:00:01,000", "srt", 1_000),
        ("00:01:00,500", "srt", 60_500),
        ("01:02:03,250", "srt", 3_723_250),
        ("99:59:59,999", "srt", 359_999_999),
        ("100:00:00,000", "srt", 360_000_000),
        ("00:00.000", "vtt", 0),
        ("00:01.500", "vtt", 1_500),
        ("59.999", "vtt", 59_999),
        ("01:02:03.250", "vtt", 3_723_250),
    ],
)
def test_parse_valid_timestamps(token, fmt, expected):
    assert parse_ms(token, fmt) == expected


@pytest.mark.parametrize(
    "token,fmt",
    [
        ("0:00:00,000", "srt"),       # hours must be >= 2 digits in SRT
        ("00:00:00.000", "srt"),      # dot is not an SRT separator
        ("00:00:00,00", "srt"),       # milliseconds must be 3 digits
        ("00:00:00,0000", "srt"),     # too many fractional digits
        ("00:60:00,000", "srt"),      # minute 60 out of range
        ("00:00:60,000", "srt"),      # second 60 out of range
        ("00:00:00", "srt"),          # missing fraction
        ("00:00:00,000 extra", "srt"),
        ("60.000", "vtt"),            # bare seconds must be < 60
        ("1:2.300", "vtt"),           # MM/SS two digits
        ("00:00:00,000", "vtt"),      # comma is not a VTT separator
        ("00:00:00.00", "vtt"),
        ("-1:00:00.000", "vtt"),
        ("", "srt"),
        ("garbage", "vtt"),
    ],
)
def test_parse_invalid_timestamps_raises(token, fmt):
    with pytest.raises(TimeParseError):
        parse_ms(token, fmt)


def test_format_roundtrip():
    for ms in (0, 1, 999, 1_000, 59_999, 3_723_250, 359_999_999):
        assert parse_ms(format_ms(ms, "srt"), "srt") == ms
        assert parse_ms(format_ms(ms, "vtt"), "vtt") == ms


def test_format_uses_correct_separator():
    assert format_ms(1500, "srt") == "00:00:01,500"
    assert format_ms(1500, "vtt") == "00:00:01.500"


def test_format_negative_rejected():
    with pytest.raises(ValueError):
        format_ms(-1, "srt")
