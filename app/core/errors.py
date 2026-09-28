"""Typed failure categories for the unification kernel.

Every error class carries a stable ``code`` that appears in API responses and in
SQLite job rows, so a failure can always be correlated with a precise category
instead of a generic 500/"success".
"""
from __future__ import annotations

from dataclasses import dataclass, field


class DicunifyError(Exception):
    """Base class for all expected, classified failures."""

    code = "INTERNAL_ERROR"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class EmptyRequestError(DicunifyError):
    code = "EMPTY_REQUEST"
    http_status = 400


class DuplicateBatchIdError(DicunifyError):
    code = "DUPLICATE_BATCH_ID"
    http_status = 400


class UnsupportedValueTypeError(DicunifyError):
    code = "UNSUPPORTED_VALUE_TYPE"
    http_status = 400


class UnsupportedIndexWidthError(DicunifyError):
    code = "UNSUPPORTED_INDEX_WIDTH"
    http_status = 400


class IndexWidthOverflowError(DicunifyError):
    """Global cardinality does not fit a client-requested strict width."""

    code = "INDEX_WIDTH_OVERFLOW"
    http_status = 422


class CardinalityLimitError(DicunifyError):
    """Global cardinality exceeds the service-wide hard ceiling."""

    code = "CARDINALITY_LIMIT_EXCEEDED"
    http_status = 422


class NullDictionaryEntryError(DicunifyError):
    code = "NULL_DICTIONARY_ENTRY"
    http_status = 400


class ValueTypeMismatchError(DicunifyError):
    code = "VALUE_TYPE_MISMATCH"
    http_status = 400


class DuplicateValueInDictionaryError(DicunifyError):
    code = "DUPLICATE_VALUE_IN_DICTIONARY"
    http_status = 400


class InvalidIndexError(DicunifyError):
    """An index is negative or not an integer."""

    code = "INVALID_INDEX"
    http_status = 400


class IndexOutOfRangeError(DicunifyError):
    code = "INDEX_OUT_OF_RANGE"
    http_status = 400


class InvalidValidityError(DicunifyError):
    code = "INVALID_VALIDITY"
    http_status = 400


class MalformedBatchError(DicunifyError):
    code = "MALFORMED_BATCH"
    http_status = 400


class VerificationMismatchError(DicunifyError):
    """The independent round-trip check found the decode differs from input."""

    code = "VERIFICATION_MISMATCH"
    http_status = 500


@dataclass
class Failure:
    """Structured form of a DicunifyError for envelopes and persistence."""

    code: str
    message: str
    details: dict = field(default_factory=dict)

    @staticmethod
    def from_exc(exc: DicunifyError) -> "Failure":
        return Failure(code=exc.code, message=exc.message, details=exc.details)
