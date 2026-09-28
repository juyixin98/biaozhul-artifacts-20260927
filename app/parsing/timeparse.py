"""Strict timestamp parsing/formatting for SRT and the WebVTT subset.

Strictness rules (anything else raises SubtitleParseError code BAD_TIMESTAMP):
  SRT: HH:MM:SS,mmm  -- hours 2+ digits, MM/SS 00-59, exactly 3 millis digits
  VTT: [HH:]MM:SS.mmm -- MM/SS always two digits 00-59, dot separator
"""
import re

from app.errors import SubtitleParseError

_SRT_TS = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$")
_VTT_TS = re.compile(r"^(?:(\d{2,}):)?([0-5]\d):([0-5]\d)\.(\d{3})$")


def parse_timestamp(token, fmt, *, line_no=None, line_text=None):
    """Parse one timestamp token to integer milliseconds."""
    rx = _SRT_TS if fmt == "srt" else _VTT_TS
    m = rx.match(token)
    if not m:
        raise SubtitleParseError(
            "BAD_TIMESTAMP",
            f"invalid {fmt} timestamp {token!r}",
            line_no=line_no,
            line_text=line_text,
        )
    if fmt == "srt":
        hours, minutes, seconds, millis = (int(g) for g in m.groups())
    else:
        hours = int(m.group(1)) if m.group(1) else 0
        minutes, seconds, millis = int(m.group(2)), int(m.group(3)), int(m.group(4))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def format_timestamp(ms, fmt):
    """Render integer milliseconds back to the canonical timestamp form."""
    ms = int(ms)
    if ms < 0:
        raise ValueError(f"cannot format negative timestamp {ms}")
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    sep = "," if fmt == "srt" else "."
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def parse_timing_line(line, fmt, *, line_no):
    """Parse a 'start --> end [settings]' line.

    Returns (start_ms, end_ms, settings). ``settings`` is the raw remainder
    after the end timestamp (e.g. WebVTT cue settings), preserved verbatim.
    """
    if "-->" not in line:
        raise SubtitleParseError(
            "BAD_TIMING",
            f"expected a 'start --> end' timing line, got {line!r}",
            line_no=line_no,
            line_text=line,
        )
    left, right = line.split("-->", 1)
    start_tok = left.strip()
    end_tok, _, settings = right.strip().partition(" ")
    start_ms = parse_timestamp(start_tok, fmt, line_no=line_no, line_text=line)
    end_ms = parse_timestamp(end_tok, fmt, line_no=line_no, line_text=line)
    return start_ms, end_ms, settings.strip()
