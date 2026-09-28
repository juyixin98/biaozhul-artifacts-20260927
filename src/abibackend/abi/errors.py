"""Typed error taxonomy for the ABI codec.

Each decode failure maps to a specific category. Callers (HTTP layer, tests) can
branch on the exact class; ``error_code`` is a stable wire string. We never
collapse an unknown/exceptional state into a success.
"""
from __future__ import annotations


class ABIError(Exception):
    """Base class for every codec error."""

    error_code = "abi_error"


class UnsupportedType(ABIError):
    error_code = "unsupported_type"


class InvalidType(ABIError):
    error_code = "invalid_type"


class ValueOutOfRange(ABIError):
    error_code = "value_out_of_range"


class NonCanonicalPadding(ABIError):
    """High/left padding must be zero (unsigned/bytes); signed must be sign-extended."""

    error_code = "non_canonical_padding"


class OffsetOutOfBounds(ABIError):
    """An offset points before its head, into a prior field, or past the blob."""

    error_code = "offset_out_of_bounds"


class OffsetOverlap(ABIError):
    """A dynamic body claims bytes already claimed by another body (offset overlap)."""

    error_code = "offset_overlap"


class LengthTooLarge(ABIError):
    """Declared length/count would exceed the allocator or arithmetic bounds."""

    error_code = "length_too_large"


class TrailingBytes(ABIError):
    """Top-level blob contains bytes the type does not consume."""

    error_code = "trailing_bytes"


class NonCanonicalEncoding(ABIError):
    """Decodable but non-canonical layout: gaps, non-minimal placement, etc."""

    error_code = "non_canonical_encoding"


class DecodeError(ABIError):
    """Generic structural failure not covered by a more specific category."""

    error_code = "decode_error"
