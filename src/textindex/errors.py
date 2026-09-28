"""Error contract shared by every module.

All service-level failures are subclasses of :class:`TextIndexError` carrying:

* ``category`` — one of four coarse failure classes the API and tests assert on
* ``code``     — stable machine-readable snake_case code (never renamed)
* ``http_status`` — the HTTP status used when raised through the API
* ``details``  — structured, JSON-serializable context for replay/diagnostics

The four categories are deliberately distinct so callers never have to parse
messages to tell *what kind* of failure happened:

* ``input_error``        — caller gave malformed/illegal input
* ``state_conflict``     — request contradicts stored state (versions, ids, …)
* ``resource_exhausted`` — a configured hard limit / OS limit was reached
* ``computation_failure``— internal invariant broken (corruption, unexpected
                           library error); nothing the caller can fix by
                           changing arguments
"""

from __future__ import annotations

from typing import Any

# --- categories -----------------------------------------------------------

CATEGORY_INPUT_ERROR = "input_error"
CATEGORY_STATE_CONFLICT = "state_conflict"
CATEGORY_RESOURCE_EXHAUSTED = "resource_exhausted"
CATEGORY_COMPUTATION_FAILURE = "computation_failure"

CATEGORIES = (
    CATEGORY_INPUT_ERROR,
    CATEGORY_STATE_CONFLICT,
    CATEGORY_RESOURCE_EXHAUSTED,
    CATEGORY_COMPUTATION_FAILURE,
)

# --- stable error codes ----------------------------------------------------

INVALID_UTF8 = "invalid_utf8"
UNPAIRED_SURROGATE = "unpaired_surrogate"
INVALID_UNICODE_ESCAPE = "invalid_unicode_escape"
UNSUPPORTED_NORMALIZATION = "unsupported_normalization"
EMPTY_DOCUMENT = "empty_document"
POSITION_OUT_OF_RANGE = "position_out_of_range"
ILLEGAL_BYTE_BOUNDARY = "illegal_byte_boundary"
ILLEGAL_CODEPOINT_BOUNDARY = "illegal_codepoint_boundary"
ILLEGAL_GRAPHEME_BOUNDARY = "illegal_grapheme_boundary"
EDIT_RANGE_CROSSED = "edit_range_crossed"
INVALID_UNIT = "invalid_unit"

DOCUMENT_NOT_FOUND = "document_not_found"
DOCUMENT_ALREADY_EXISTS = "document_already_exists"
DIGEST_MISMATCH = "digest_mismatch"
INDEX_VERSION_MISMATCH = "index_version_mismatch"
INDEX_CORRUPT = "index_corrupt"
CONFLICT_RETRY = "conflict_retry"

DOCUMENT_TOO_LARGE = "document_too_large"
TOO_MANY_CLUSTERS = "too_many_clusters"
STORAGE_FULL = "storage_full"

INTERNAL_ERROR = "internal_error"
SEGMENTER_ERROR = "segmenter_error"


class TextIndexError(Exception):
    """Base class for every defined service error."""

    category: str = CATEGORY_COMPUTATION_FAILURE
    code: str = INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation used by API and log records."""
        return {
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


# --- input_error (4xx) -----------------------------------------------------


class InvalidUtf8(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = INVALID_UTF8
    http_status = 400

    def __init__(self, reason: str, start: int, end: int, raw_hex: str) -> None:
        super().__init__(
            f"invalid UTF-8 at byte {start}: {reason}",
            details={"reason": reason, "start_byte": start, "end_byte": end,
                     "raw_hex": raw_hex},
        )


class UnpairedSurrogate(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = UNPAIRED_SURROGATE
    http_status = 400

    def __init__(self, codepoint: int, at: int) -> None:
        super().__init__(
            f"unpaired surrogate U+{codepoint:04X} at codepoint offset {at}",
            details={"codepoint": f"U+{codepoint:04X}", "codepoint_offset": at},
        )


class InvalidUnicodeEscape(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = INVALID_UNICODE_ESCAPE
    http_status = 400


class UnsupportedNormalization(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = UNSUPPORTED_NORMALIZATION
    http_status = 400

    def __init__(self, requested: str, supported: list[str]) -> None:
        super().__init__(
            f"unsupported normalization form {requested!r}",
            details={"requested": requested, "supported": supported},
        )


class EmptyDocument(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = EMPTY_DOCUMENT
    http_status = 422  # syntactically fine, semantically rejected


class PositionOutOfRange(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = POSITION_OUT_OF_RANGE
    http_status = 400

    def __init__(self, unit: str, position: int, limit: int) -> None:
        super().__init__(
            f"{unit} position {position} out of range [0, {limit}]",
            details={"unit": unit, "position": position, "max": limit},
        )


class IllegalByteBoundary(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = ILLEGAL_BYTE_BOUNDARY
    http_status = 422

    def __init__(self, byte_offset: int) -> None:
        super().__init__(
            f"byte offset {byte_offset} is not a UTF-8 lead boundary",
            details={"byte_offset": byte_offset},
        )


class IllegalCodepointBoundary(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = ILLEGAL_CODEPOINT_BOUNDARY
    http_status = 422

    def __init__(self, codepoint_offset: int) -> None:
        super().__init__(
            f"codepoint offset {codepoint_offset} is not a grapheme boundary",
            details={"codepoint_offset": codepoint_offset},
        )


class IllegalGraphemeBoundary(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = ILLEGAL_GRAPHEME_BOUNDARY
    http_status = 422

    def __init__(self, cluster_index: int, limit: int) -> None:
        super().__init__(
            f"grapheme cluster index {cluster_index} out of range [0, {limit}]",
            details={"cluster_index": cluster_index, "max": limit},
        )


class EditRangeCrossed(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = EDIT_RANGE_CROSSED
    http_status = 400

    def __init__(self, start: int, end: int, unit: str) -> None:
        super().__init__(
            f"edit range start {start} > end {end} ({unit})",
            details={"start": start, "end": end, "unit": unit},
        )


class InvalidUnit(TextIndexError):
    category = CATEGORY_INPUT_ERROR
    code = INVALID_UNIT
    http_status = 400

    def __init__(self, unit: str) -> None:
        super().__init__(
            f"unknown position unit {unit!r}",
            details={"unit": unit, "allowed": ["byte", "codepoint", "grapheme"]},
        )


# --- state_conflict (404/409) ----------------------------------------------


class DocumentNotFound(TextIndexError):
    category = CATEGORY_STATE_CONFLICT
    code = DOCUMENT_NOT_FOUND
    http_status = 404

    def __init__(self, doc_id: str) -> None:
        super().__init__(f"document {doc_id!r} not found",
                         details={"doc_id": doc_id})


class DocumentAlreadyExists(TextIndexError):
    category = CATEGORY_STATE_CONFLICT
    code = DOCUMENT_ALREADY_EXISTS
    http_status = 409

    def __init__(self, doc_id: str) -> None:
        super().__init__(f"document {doc_id!r} already exists",
                         details={"doc_id": doc_id})


class DigestMismatch(TextIndexError):
    """Edit/request claimed a base digest that does not match stored text."""

    category = CATEGORY_STATE_CONFLICT
    code = DIGEST_MISMATCH
    http_status = 409

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            "base digest does not match current document",
            details={"provided": expected, "current": actual},
        )


class IndexVersionMismatch(TextIndexError):
    """Stored blob was produced under another Unicode/blob format version."""

    category = CATEGORY_STATE_CONFLICT
    code = INDEX_VERSION_MISMATCH
    http_status = 409

    def __init__(self, stored_identity: str, current_identity: str) -> None:
        super().__init__(
            "index built under a different Unicode data version",
            details={"stored": stored_identity, "current": current_identity},
        )


class IndexCorrupt(TextIndexError):
    """Stored blob fails structural or checksum validation."""

    category = CATEGORY_COMPUTATION_FAILURE
    code = INDEX_CORRUPT
    http_status = 500

    def __init__(self, reason: str, **extra: Any) -> None:
        super().__init__(
            f"stored index is corrupt: {reason}",
            details={"reason": reason, **extra},
        )


# --- resource_exhausted (413/507) ------------------------------------------


class DocumentTooLarge(TextIndexError):
    category = CATEGORY_RESOURCE_EXHAUSTED
    code = DOCUMENT_TOO_LARGE
    http_status = 413

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"document payload {size} bytes exceeds limit {limit}",
            details={"size": size, "limit": limit},
        )


class TooManyClusters(TextIndexError):
    category = CATEGORY_RESOURCE_EXHAUSTED
    code = TOO_MANY_CLUSTERS
    http_status = 413

    def __init__(self, count: int, limit: int) -> None:
        super().__init__(
            f"grapheme cluster count {count} exceeds limit {limit}",
            details={"clusters": count, "limit": limit},
        )


class StorageFull(TextIndexError):
    category = CATEGORY_RESOURCE_EXHAUSTED
    code = STORAGE_FULL
    http_status = 507

    def __init__(self, sqlite_error: str, sqlite_code: int | None = None) -> None:
        super().__init__(
            f"storage backend full: {sqlite_error}",
            details={"sqlite_error": sqlite_error, "sqlite_code": sqlite_code},
        )


# --- computation_failure (500) ---------------------------------------------


class SegmenterError(TextIndexError):
    category = CATEGORY_COMPUTATION_FAILURE
    code = SEGMENTER_ERROR
    http_status = 500

    def __init__(self, error: str) -> None:
        super().__init__(f"segmentation library failure: {error}",
                         details={"error": error})


class InternalError(TextIndexError):
    category = CATEGORY_COMPUTATION_FAILURE
    code = INTERNAL_ERROR
    http_status = 500
