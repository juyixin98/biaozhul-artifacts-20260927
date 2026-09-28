"""Canonical domain values shared by predicates and file statistics.

All comparable values are normalized into one of five ordered domains so that a
predicate literal and a min/max read from a Parquet file can never be compared
"bucket number vs original value" style: they are first put on the *same*
domain, or the comparison is rejected as a type mismatch (which the kernel
treats conservatively).

Domains:
  * int      - Python int (also epoch seconds/micros input)
  * float    - Python float
  * str      - Python str (also date "YYYY-MM-DD" when column is DATE)
  * bool     - Python bool
  * datetime - timezone-aware UTC datetime.datetime
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Optional

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

INT = "int"
FLOAT = "float"
STR = "str"
BOOL = "bool"
DATETIME = "datetime"
DATE = "date"  # logical column type; values live on the STR domain


class ValueError(ValueError):
    """Raised when a literal cannot be normalized on its declared domain."""


def _parse_iso_datetime(text: str) -> _dt.datetime:
    s = text.strip()
    # datetime.fromisoformat in 3.12 accepts 'Z'; normalize anyway for clarity.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = _dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        # Bare local wall-clock is ambiguous without a zone: treat as UTC and
        # record the assumption explicitly, rather than silently using host tz.
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_date(text: str) -> str:
    m = _DATE_RE.match(text)
    if not m:
        raise ValueError(f"not a YYYY-MM-DD date: {text!r}")
    y, mo, da = (int(x) for x in m.groups())
    # validate the calendar date
    _dt.date(y, mo, da)
    return f"{y:04d}-{mo:02d}-{da:02d}"


def to_epoch_us(d: _dt.datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    delta = d - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def epoch_us_to_datetime(us: int) -> _dt.datetime:
    return _EPOCH + _dt.timedelta(microseconds=us)


def canonical(value: Any, domain: str) -> Any:
    """Normalize an external (JSON/Parquet) value onto a domain.

    ``domain`` is the column type tag (INT/FLOAT/STR/BOOL/DATE/DATETIME).
    DATETIME accepts: aware/naive ISO-8601 strings, int epoch seconds,
    int epoch microseconds (>= 10**13 or <= -10**13 heuristically), floats.
    """
    if value is None:
        return None

    if domain == DATETIME:
        if isinstance(value, _dt.datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=_dt.timezone.utc)
            return value.astimezone(_dt.timezone.utc)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            v = float(value)
            # Heuristic fixed in one place: |v| >= 1e13 is microseconds.
            unit = 1_000_000 if abs(v) >= 1e13 else 1
            return _EPOCH + _dt.timedelta(seconds=v / unit)
        if isinstance(value, str):
            return _parse_iso_datetime(value)
        raise ValueError(f"cannot interpret {value!r} as timestamp")

    if domain == DATE:
        if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
            return value.isoformat()
        if isinstance(value, str):
            return parse_date(value)
        raise ValueError(f"cannot interpret {value!r} as date")

    if domain == INT:
        if isinstance(value, bool):
            raise ValueError("bool is not an int literal here")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"cannot interpret {value!r} as int")

    if domain == FLOAT:
        if isinstance(value, bool):
            raise ValueError("bool is not a float literal here")
        if isinstance(value, (int, float)):
            return float(value)
        raise ValueError(f"cannot interpret {value!r} as float")

    if domain == BOOL:
        if isinstance(value, bool):
            return value
        raise ValueError(f"cannot interpret {value!r} as bool")

    if domain == STR:
        if isinstance(value, str):
            return value
        raise ValueError(f"cannot interpret {value!r} as str")

    raise ValueError(f"unknown domain {domain!r}")


def json_default(value: Any) -> str:
    """JSON encoder helper: datetimes -> ISO UTC 'Z' form."""
    if isinstance(value, _dt.datetime):
        d = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return d.isoformat() + "Z"
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def same_domain(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, _dt.datetime) or isinstance(b, _dt.datetime):
        return isinstance(a, _dt.datetime) and isinstance(b, _dt.datetime)
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and not (
        isinstance(a, bool) or isinstance(b, bool)
    ) or (isinstance(a, str) and isinstance(b, str))


def cmp(a: Any, b: Any) -> Optional[int]:
    """Compare two canonical values; None when domains do not match."""
    if not same_domain(a, b):
        # int/float cross compare numerically; same_domain already allows that.
        return None
    if a < b:
        return -1
    if a > b:
        return 1
    return 0
