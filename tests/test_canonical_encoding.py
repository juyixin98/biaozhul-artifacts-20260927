"""Canonical encoding rules — positive vectors and concrete failure classes."""
from __future__ import annotations

import datetime as dt

import pytest

from app.domain.types import CanonicalEncodeError, FieldType, canonical_encode


def test_type_tags_make_equal_typed_values_distinct():
    assert canonical_encode(FieldType.INT, 7) != canonical_encode(FieldType.STRING, "7")
    assert canonical_encode(FieldType.INT, 1) != canonical_encode(FieldType.BOOL, True)
    assert (canonical_encode(FieldType.DECIMAL, "12.30")
            != canonical_encode(FieldType.DECIMAL, "12.3"))  # exponent differs


def test_empty_null_missing_are_three_states():
    empty = canonical_encode(FieldType.STRING, "")
    null = canonical_encode(FieldType.NULL, None)
    assert empty != null
    # missing marker comes from its own helper, never coinciding with a value
    from app.domain.types import canonical_encode_missing
    missing = canonical_encode_missing()
    assert missing not in (empty, null)


def test_decimal_rejects_float_binary_rounding():
    with pytest.raises(CanonicalEncodeError) as exc:
        canonical_encode(FieldType.DECIMAL, 1.5)
    assert "float" in str(exc.value)


def test_timestamp_requires_timezone():
    with pytest.raises(CanonicalEncodeError) as exc:
        canonical_encode(FieldType.TIMESTAMP, dt.datetime(2026, 1, 1, 9, 0, 0))
    assert "timezone-aware" in str(exc.value)
    # Aware timestamps in different zones agree on the same instant.
    z = canonical_encode(FieldType.TIMESTAMP, "2026-03-01T09:00:00+00:00")
    shifted = canonical_encode(FieldType.TIMESTAMP, "2026-03-01T17:00:00+08:00")
    assert z == shifted


def test_null_is_explicit_for_every_type_and_distinct_from_missing():
    null_string = canonical_encode(FieldType.STRING, None)
    null_int = canonical_encode(FieldType.INT, None)
    null_bool = canonical_encode(FieldType.BOOL, None)
    null_typed = canonical_encode(FieldType.NULL, None)
    # One canonical null regardless of declared type.
    assert null_string == null_int == null_bool == null_typed
    from app.domain.types import canonical_encode_missing
    assert null_string != canonical_encode_missing()


def test_bad_literals_raise_not_succeed():
    with pytest.raises(CanonicalEncodeError):
        canonical_encode(FieldType.INT, "12abc")
    with pytest.raises(CanonicalEncodeError):
        canonical_encode(FieldType.DATE, "01/03/2026")
    with pytest.raises(CanonicalEncodeError):
        canonical_encode(FieldType.STRING, 123)
    with pytest.raises(CanonicalEncodeError):
        canonical_encode(FieldType.BOOL, 1)


def test_field_name_validation():
    from app.domain.types import canonical_validate_field_name
    for bad in ("", "a.b", "a\x00b", "a b"):
        with pytest.raises(CanonicalEncodeError):
            canonical_validate_field_name(bad)
