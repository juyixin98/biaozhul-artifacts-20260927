"""Canonical, type-tagged encoding of committed field values.

Design rules
------------
* Every scalar is encoded as ``(TYPE_TAG, raw_payload_bytes)``. The *type* is
  therefore bound into the commitment: the same lexical value under different
  declared types yields different commitments.
* Presence is explicit and separate from payload. The three committed states
  are ``present``, ``null`` and ``missing``. ``null`` (a known-but-empty value)
  and ``missing`` (no value committed at all) must never collide, and an empty
  string must never collide with null.
* Encodings are canonical: one accepted wire/JSON value has exactly one byte
  representation. Anything ambiguous (NaNs, exponents in decimals,
  float-typed integers, booleans passed as ints) is rejected with
  ``TYPE_ENCODING_ERROR`` rather than coerced.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import re
from typing import Any

from .errors import TypeEncodingError

# Public protocol constants (shared with the independent verifier by design).
TYPE_TEXT = "text"
TYPE_INT = "int"
TYPE_BOOL = "bool"
TYPE_DECIMAL = "decimal"
TYPE_DATE = "date"
TYPE_TIMESTAMP = "timestamp"
FIELD_TYPES = frozenset(
    {TYPE_TEXT, TYPE_INT, TYPE_BOOL, TYPE_DECIMAL, TYPE_DATE, TYPE_TIMESTAMP}
)

STATE_PRESENT = "present"
STATE_NULL = "null"
STATE_MISSING = "missing"
COMMITTED_STATES = frozenset({STATE_PRESENT, STATE_NULL, STATE_MISSING})

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _check_text(v: Any) -> bytes:
    if not isinstance(v, str):
        raise TypeEncodingError(
            f"text field requires a JSON string, got {type(v).__name__}"
        )
    # UTF-8 is canonical for Python unicode -> bytes; no normalisation is
    # applied so NFC/NFD distinctions are preserved rather than collapsed.
    return v.encode("utf-8")


def _check_int(v: Any) -> bytes:
    # bool is a subclass of int in Python; reject it explicitly so True cannot
    # be committed as integer 1 under a swapped type.
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeEncodingError(
            f"int field requires a JSON integer, got {type(v).__name__}"
        )
    return str(v).encode("ascii")


def _check_bool(v: Any) -> bytes:
    if not isinstance(v, bool):
        raise TypeEncodingError(
            f"bool field requires a JSON boolean, got {type(v).__name__}"
        )
    return b"1" if v else b"0"


def _canonical_decimal(v: Any) -> bytes:
    if isinstance(v, bool) or isinstance(v, float):
        raise TypeEncodingError(
            "decimal must be supplied as a JSON string to remain exact, "
            f"got {type(v).__name__}"
        )
    if isinstance(v, int):
        return str(v).encode("ascii")
    if isinstance(v, decimal.Decimal):
        s = format(v, "f")
    elif isinstance(v, str):
        s = v.strip()
    else:
        raise TypeEncodingError(
            f"decimal field requires a string, got {type(v).__name__}"
        )

    if not re.fullmatch(r"[+-]?(\d+\.\d+|\d+|\.\d+)", s):
        raise TypeEncodingError(
            "decimal must be a fixed-point base-10 literal "
            "(sign, digits, at most one '.'); exponents, NaN and Infinity "
            "are rejected"
        )
    sign = ""
    body = s
    if body[0] in "+-":
        sign, body = ("-" if body[0] == "-" else ""), body[1:]
    int_part, _, frac_part = body.partition(".")
    int_part = int_part.lstrip("0") or "0"
    frac_part = frac_part.rstrip("0")
    if int_part == "0" and not frac_part:
        sign = ""  # collapse -0 / +0 to a single zero
    canon = sign + int_part + (("." + frac_part) if frac_part else "")
    return canon.encode("ascii")


def _check_date(v: Any) -> bytes:
    if not isinstance(v, str):
        raise TypeEncodingError(
            f"date field requires an ISO YYYY-MM-DD string, got {type(v).__name__}"
        )
    if not _DATE_RE.fullmatch(v):
        raise TypeEncodingError(f"date {v!r} is not in YYYY-MM-DD form")
    try:
        parsed = _dt.date.fromisoformat(v)
    except ValueError as exc:
        raise TypeEncodingError(f"date {v!r} is not a real calendar date: {exc}")
    # Re-emit from the parsed object to guarantee zero-padded canonical form.
    return parsed.isoformat().encode("ascii")


def _check_timestamp(v: Any) -> bytes:
    if isinstance(v, _dt.datetime):
        dt = v
    elif isinstance(v, str):
        text = v.strip()
        # datetime.fromisoformat accepts naive strings; require a timezone.
        try:
            dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TypeEncodingError(f"timestamp {v!r} is not ISO-8601: {exc}")
    else:
        raise TypeEncodingError(
            "timestamp requires an ISO-8601 string with timezone offset "
            f"(or 'Z'), got {type(v).__name__}"
        )
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise TypeEncodingError(
            "timestamp must carry an explicit timezone (naive timestamps are "
            "rejected so encoding stays canonical)"
        )
    # Normalise to UTC instant and serialise with a trailing Z.
    utc = dt.astimezone(_dt.timezone.utc)
    return (utc.strftime("%Y-%m-%dT%H:%M:%S") + _utc_frac(utc) + "Z").encode(
        "ascii"
    )


def _utc_frac(dt: _dt.datetime) -> str:
    micros = dt.microsecond
    return "" if micros == 0 else f".{micros:06d}".rstrip("0")


_ENCODERS = {
    TYPE_TEXT: _check_text,
    TYPE_INT: _check_int,
    TYPE_BOOL: _check_bool,
    TYPE_DECIMAL: _canonical_decimal,
    TYPE_DATE: _check_date,
    TYPE_TIMESTAMP: _check_timestamp,
}


def validate_type_name(field_type: str) -> None:
    if field_type not in FIELD_TYPES:
        raise TypeEncodingError(
            f"unknown field type {field_type!r}; allowed: "
            f"{sorted(FIELD_TYPES)}"
        )


def encode_present(field_type: str, value: Any) -> tuple[str, bytes]:
    """Return ``(TYPE_TAG, payload)`` for a present, non-null value."""
    validate_type_name(field_type)
    payload = _ENCODERS[field_type](value)
    return field_type, payload
