"""Time & signal kernel — strict timestamp parsing and formatting.

SRT form:  ``HH:MM:SS,mmm``  (comma decimal; hours >= 2 digits, MM/SS 00-59)
VTT form:  ``HH?:MM:SS.mmm`` (dot decimal; hours optional, MM/SS 00-59)

Parsing is *strict*: the whole string must match, minutes/seconds must be in
range, and exactly three fractional digits are accepted in the restricted
subset. All internal arithmetic is integer milliseconds to avoid float drift.
"""
from __future__ import annotations

import re

# Hours: SRT allows 2+ digits (long running times are legal), VTT 1-2 when given.
_SRT_RE = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$")
# VTT: HH:MM:SS.frac or MM:SS.frac (minutes 1+ digits), or bare SS.frac.
_VTT_RE_CLOCK = re.compile(r"^(?:(\d{1,}):)?(\d{1,}):([0-5]\d)\.(\d{3})$")
_VTT_RE_SECONDS = re.compile(r"^([0-5]?\d)\.(\d{3})$")


class TimeParseError(ValueError):
    """Raised when a timestamp token does not conform to the strict subset."""


def parse_ms(token: str, fmt: str) -> int:
    """Parse one timestamp token into integer milliseconds.

    Raises :class:`TimeParseError` on any deviation from the subset grammar.
    """
    if not isinstance(token, str):
        raise TimeParseError(f"timestamp must be a string, got {type(token).__name__}")
    t = token.strip()
    if fmt == "srt":
        m = _SRT_RE.match(t)
        if m is None:
            raise TimeParseError(
                f"invalid SRT timestamp {token!r}: expected 'HH:MM:SS,mmm'")
        hh, mm, ss, ms = (int(g) for g in m.groups())
    else:
        mc = _VTT_RE_CLOCK.match(t)
        if mc is not None:
            h_s, mm_s, ss_s, ms_s = mc.groups()
            hh = int(h_s) if h_s is not None else 0
            mm, ss, ms = int(mm_s), int(ss_s), int(ms_s)
        else:
            ms_ = _VTT_RE_SECONDS.match(t)
            if ms_ is None:
                raise TimeParseError(
                    f"invalid VTT timestamp {token!r}: expected 'MM:SS.mmm' "
                    f"or 'HH:MM:SS.mmm' or 'SS.mmm'")
            hh, mm = 0, 0
            ss, ms = int(ms_.group(1)), int(ms_.group(2))
    return hh * 3_600_000 + mm * 60_000 + ss * 1_000 + ms


def format_ms(ms: int, fmt: str) -> str:
    """Render integer milliseconds in SRT (comma) or VTT (dot) notation."""
    if ms < 0:
        raise ValueError(f"cannot format negative time {ms}")
    hh, rem = divmod(ms, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, frac = divmod(rem, 1_000)
    sep = "," if fmt == "srt" else "."
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{frac:03d}"
