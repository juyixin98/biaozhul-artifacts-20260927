"""Domain error categories.

Failures are *categorised* rather than surfaced as generic 500s. Each category
maps to a stable string code so API clients and tests can assert on the exact
failure class (``offsets_not_monotonic`` rather than "something went wrong").
"""
from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    # Input decoding / shape problems.
    MALFORMED_PAYLOAD = "malformed_payload"
    UNSUPPORTED_TYPE = "unsupported_type"
    MISSING_BUFFER = "missing_buffer"
    EMPTY_BUFFER = "empty_buffer"
    BUFFER_TOO_SHORT = "buffer_too_short"
    BUFFER_NOT_ALIGNED = "buffer_not_aligned"
    # Validity bitmap category.
    VALIDITY_TOO_SHORT = "validity_too_short"
    VALIDITY_TRAILING_BITS_SET = "validity_trailing_bits_set"
    NULL_COUNT_MISMATCH = "null_count_mismatch"
    # Offset category (variable-length strings).
    OFFSETS_TOO_SHORT = "offsets_too_short"
    OFFSET_NOT_ZERO = "offset_not_zero"
    OFFSETS_NOT_MONOTONIC = "offsets_not_monotonic"
    OFFSET_OUT_OF_RANGE = "offset_out_of_range"
    # Data category.
    DATA_TOO_SHORT = "data_too_short"
    # Semantic scan (reading every value end-to-end).
    SEMANTIC_SCAN_FAILED = "semantic_scan_failed"
    # Kernel rules.
    TYPE_MISMATCH = "type_mismatch"
    SLICE_OUT_OF_RANGE = "slice_out_of_range"
    NOT_FOUND = "not_found"
    # Anything we did not anticipate; never silently mapped to success.
    INTERNAL_ERROR = "internal_error"


class LayoutError(ValueError):
    """Raised by kernels/adapters when a buffer layout is unusable."""

    def __init__(self, category: ErrorCategory, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"category": self.category.value, "message": self.message, "detail": self.detail}
