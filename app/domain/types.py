"""Typed field model and canonical byte encoding (rule/evidence parsing layer).

Commitments are only meaningful if two honest participants serialise the same
typed value to the *same* bytes. This module is the single authoritative
encoding rule for the service core:

  * values are typed explicitly (never guessed from a Python object),
  * every field carries a fixed one-byte type tag,
  * variable-length payloads are length-prefixed (32-bit big-endian),
  * null, empty string and "field absent" are three distinct encodings,
  * decimal uses a decimal exponent so trailing zeros are preserved,
  * text is normalised to NFC UTF-8,
  * datetimes must be timezone-aware (naive timestamps are rejected rather
    than silently reinterpreted — unknown state must never look like success).

The independent verifier (app/verifier) re-derives an equivalent encoder on
its own instead of importing this module; tests/fixtures cross-check the two.
"""
from __future__ import annotations

import datetime as _dt
import unicodedata
from decimal import Decimal, InvalidOperation
from enum import Enum

import struct as _struct


class FieldType(str, Enum):
    STRING = "string"
    INT = "int"
    DECIMAL = "decimal"
    BOOL = "bool"
    DATE = "date"
    TIMESTAMP = "timestamp"
    NULL = "null"


# One-byte domain tags. Fixed for v1; a schema version change must accompany
# any change here.
_TYPE_TAGS: dict[FieldType, int] = {
    FieldType.STRING: 0x10,
    FieldType.INT: 0x11,
    FieldType.DECIMAL: 0x12,
    FieldType.BOOL: 0x13,
    FieldType.DATE: 0x14,
    FieldType.TIMESTAMP: 0x15,
    FieldType.NULL: 0x1F,
}


class CanonicalEncodeError(ValueError):
    """Raised when a raw value cannot be canonically encoded for its type."""


def lp(payload: bytes) -> bytes:
    """Unsigned 32-bit big-endian length prefix."""
    n = len(payload)
    if n > 0xFFFFFFFF:
        raise CanonicalEncodeError(f"payload too large for length prefix: {n}")
    return _struct.pack(">I", n) + payload


def _encode_string(value: object) -> bytes:
    if not isinstance(value, str):
        raise CanonicalEncodeError(f"string field requires str, got {type(value).__name__}")
    normalised = unicodedata.normalize("NFC", value)
    return lp(normalised.encode("utf-8"))


def _encode_int(value: object) -> bytes:
    if isinstance(value, bool):
        # bool is an int subclass in Python; accepting it would silently
        # collapse True/1 into one commitment, so reject it explicitly.
        raise CanonicalEncodeError("int field rejects bool")
    if isinstance(value, int):
        integer = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            integer = int(text)
        except ValueError as exc:
            raise CanonicalEncodeError(f"not an integer literal: {value!r}") from exc
    else:
        raise CanonicalEncodeError(f"int field requires int/str, got {type(value).__name__}")
    if not (-(2**63) <= integer < 2**63):
        raise CanonicalEncodeError(f"integer out of signed 64-bit range: {integer}")
    return _struct.pack(">q", integer)


def _encode_decimal(value: object) -> bytes:
    if isinstance(value, bool):
        raise CanonicalEncodeError("decimal field rejects bool")
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, (int, str)):
        try:
            dec = Decimal(str(value))
        except InvalidOperation as exc:
            raise CanonicalEncodeError(f"not a decimal literal: {value!r}") from exc
    elif isinstance(value, float):
        # Float input would smuggle binary rounding into a commitment; refuse.
        raise CanonicalEncodeError("decimal field rejects float; send a string or int")
    else:
        raise CanonicalEncodeError(f"decimal field requires str/int/Decimal, got {type(value).__name__}")
    if not dec.is_finite():
        raise CanonicalEncodeError(f"decimal must be finite: {dec}")
    sign, digits, exponent = dec.as_tuple()
    coefficient = 0
    for d in digits:
        coefficient = coefficient * 10 + d
    if sign:
        coefficient = -coefficient
    if not (-1_000_000 <= exponent <= 0):
        raise CanonicalEncodeError(f"decimal exponent out of supported range: {exponent}")
    if not (-(2**63) <= coefficient < 2**63):
        raise CanonicalEncodeError(f"decimal coefficient out of range: {coefficient}")
    # >q coefficient, >i negative decimal exponent (e.g. "12.30" -> 1230, -2)
    return _struct.pack(">qi", coefficient, -exponent)


def _encode_bool(value: object) -> bytes:
    if not isinstance(value, bool):
        raise CanonicalEncodeError(f"bool field requires bool, got {type(value).__name__}")
    return b"\x01" if value else b"\x00"


def _encode_date(value: object) -> bytes:
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        date = value
    elif isinstance(value, str):
        try:
            date = _dt.date.fromisoformat(value)
        except ValueError as exc:
            raise CanonicalEncodeError(f"not an ISO date: {value!r}") from exc
    else:
        raise CanonicalEncodeError(f"date field requires date/ISO str, got {type(value).__name__}")
    return _struct.pack(">hhh", date.year, date.month, date.day)


def _encode_timestamp(value: object) -> bytes:
    if isinstance(value, _dt.datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00") if value.strip().endswith("Z") else value
        try:
            moment = _dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise CanonicalEncodeError(f"not an ISO timestamp: {value!r}") from exc
    else:
        raise CanonicalEncodeError(
            f"timestamp field requires datetime/ISO str, got {type(value).__name__}"
        )
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise CanonicalEncodeError("timestamp must be timezone-aware (naive values rejected)")
    utc = moment.astimezone(_dt.timezone.utc)
    epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    micros = int((utc - epoch) // _dt.timedelta(microseconds=1))
    return _struct.pack(">q", micros)


def _encode_null(value: object) -> bytes:
    if value is not None:
        raise CanonicalEncodeError(f"null field requires None, got {type(value).__name__}")
    return b""


_ENCODERS = {
    FieldType.STRING: _encode_string,
    FieldType.INT: _encode_int,
    FieldType.DECIMAL: _encode_decimal,
    FieldType.BOOL: _encode_bool,
    FieldType.DATE: _encode_date,
    FieldType.TIMESTAMP: _encode_timestamp,
    FieldType.NULL: _encode_null,
}


# Distinct encodings of "absence".
MISSING_PAYLOAD = b"\xff\xff"  # field declared in schema but absent on the record


def canonical_validate_field_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise CanonicalEncodeError("field name must be a non-empty string")
    if "." in name or "\x00" in name:
        raise CanonicalEncodeError(f"field name must not contain '.' or NUL: {name!r}")
    if any(ch.isspace() for ch in name):
        raise CanonicalEncodeError(f"field name must not contain whitespace: {name!r}")


def canonical_encode(field_type: FieldType | str, value: object) -> bytes:
    """Encode one typed value as tag + canonical payload bytes.

    ``None`` is an explicit SQL-style NULL valid for every field type and
    always encodes to the single null tag, independent of the declared type.
    "Field absent" is a separate encoding (canonical_encode_missing).
    """
    try:
        ft = field_type if isinstance(field_type, FieldType) else FieldType(field_type)
    except ValueError as exc:
        raise CanonicalEncodeError(f"unknown field type: {field_type!r}") from exc
    if value is None:
        return bytes([_TYPE_TAGS[FieldType.NULL]])
    return bytes([_TYPE_TAGS[ft]]) + _ENCODERS[ft](value)


def canonical_encode_missing() -> bytes:
    """Encoding for a schema-declared field that is absent from a record."""
    return MISSING_PAYLOAD
