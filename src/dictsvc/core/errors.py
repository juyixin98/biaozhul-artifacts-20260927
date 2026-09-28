"""Categorized errors.

Every failure has a stable ``category`` (part of the API contract) so that
clients and tests can assert the *failure category*, never a vague 200.
Unknown/unexpected states are reported as INTERNAL_ERROR, never coerced
into success.
"""
from __future__ import annotations


class DictSvcError(Exception):
    """Base class for all classified service errors."""

    category = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str, *, batch_id: str | None = None,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.batch_id = batch_id
        self.details = details or {}

    def to_dict(self) -> dict:
        d = {"ok": False, "error": {
            "category": self.category,
            "message": self.message,
        }}
        if self.batch_id is not None:
            d["error"]["batch_id"] = self.batch_id
        if self.details:
            d["error"]["details"] = self.details
        return d


class RequestMalformed(DictSvcError):
    """Structural input problem (wrong types/shapes); the request cannot run."""
    category = "REQUEST_MALFORMED"
    http_status = 400


class DictionaryContainsNull(DictSvcError):
    """A null slot appeared inside a declared dictionary value.

    NULL is represented exclusively by the index validity bitmap and must
    never occupy an ordinary dictionary value code.
    """
    category = "DICTIONARY_CONTAINS_NULL"
    http_status = 422


class IndexOutOfRange(DictSvcError):
    """An index at a *valid* (non-NULL) row points outside its local dict."""
    category = "INDEX_OUT_OF_RANGE"
    http_status = 422


class DuplicateBatchId(DictSvcError):
    """Two batches in one request claim the same batch_id."""
    category = "DUPLICATE_BATCH_ID"
    http_status = 422


class UnsupportedValueType(DictSvcError):
    """Dictionary values have a type the kernel does not support."""
    category = "UNSUPPORTED_VALUE_TYPE"
    http_status = 422


class DuplicateDictionaryValue(DictSvcError):
    """Strict mode: the same local code was bound to two distinct values,
    or conflicting duplicate values appeared in one local dictionary."""
    category = "DUPLICATE_DICTIONARY_VALUE"
    http_status = 422


class CardinalityOverflow(DictSvcError):
    """Global cardinality exceeds the target index bit width and the policy
    is ``reject`` (or the fixed ladder cannot grow further)."""
    category = "CARDINALITY_OVERFLOW"
    http_status = 422


class RunConflict(DictSvcError):
    """A run with the same id already exists in the metadata store."""
    category = "RUN_CONFLICT"
    http_status = 409


class RunNotFound(DictSvcError):
    category = "RUN_NOT_FOUND"
    http_status = 404
