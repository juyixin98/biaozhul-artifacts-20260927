import pytest

from app.errors import SubtitleParseError
from app.parsing.timeparse import format_timestamp, parse_timing_line, parse_timestamp


def test_srt_timestamp_values():
    assert parse_timestamp("00:00:01,000", "srt") == 1000
    assert parse_timestamp("01:02:03,456", "srt") == 3_723_456
    assert parse_timestamp("100:00:00,000", "srt") == 360_000_000


@pytest.mark.parametrize("bad", [
    "0:00:01,000",      # hour must be 2+ digits
    "00:60:00,000",     # minute out of range
    "00:00:60,000",     # second out of range
    "00:00:01.000",     # wrong separator for SRT
    "00:00:01,00",      # millis must be exactly 3 digits
    "00:00:01,0000",
    "00:00:01",
    "abc",
    "-00:00:01,000",    # negative timestamps are not representable
])
def test_srt_timestamp_rejected(bad):
    with pytest.raises(SubtitleParseError) as ei:
        parse_timestamp(bad, "srt")
    assert ei.value.code == "BAD_TIMESTAMP"


def test_vtt_timestamp_values():
    assert parse_timestamp("00:01.500", "vtt") == 1500
    assert parse_timestamp("00:00:01.500", "vtt") == 1500
    assert parse_timestamp("02:00:00.000", "vtt") == 7_200_000


@pytest.mark.parametrize("bad", [
    "1:02.500",         # minutes must be two digits
    "00:01,500",        # comma not allowed in VTT
    "00:60.000",        # seconds out of range
    "00:00:60.000",
    "00:01.5",
])
def test_vtt_timestamp_rejected(bad):
    with pytest.raises(SubtitleParseError) as ei:
        parse_timestamp(bad, "vtt")
    assert ei.value.code == "BAD_TIMESTAMP"


def test_format_roundtrip():
    assert format_timestamp(3_723_456, "srt") == "01:02:03,456"
    assert format_timestamp(3_723_456, "vtt") == "01:02:03.456"
    assert format_timestamp(0, "srt") == "00:00:00,000"
    for fmt in ("srt", "vtt"):
        for ms in (0, 1, 999, 60_000, 3_599_999, 360_000_000):
            assert parse_timestamp(format_timestamp(ms, fmt), fmt) == ms


def test_timing_line_with_settings():
    s, e, settings = parse_timing_line(
        "00:00:01,000 --> 00:00:02,000 X1:1 X2:2", "srt", line_no=7)
    assert (s, e, settings) == (1000, 2000, "X1:1 X2:2")


def test_timing_line_missing_arrow():
    with pytest.raises(SubtitleParseError) as ei:
        parse_timing_line("00:00:01,000 00:00:02,000", "srt", line_no=3)
    assert ei.value.code == "BAD_TIMING"
    assert ei.value.line_no == 3
