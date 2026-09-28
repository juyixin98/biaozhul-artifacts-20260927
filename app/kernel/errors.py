"""Shared error taxonomy, request ids and redaction helpers.

Every failure the service can produce belongs to one of the categories below.
Tests assert on the *category*, not just on HTTP status, so that "interface
works but accepted something it should have rejected" is impossible to hide.
"""
from __future__ import annotations

import hashlib
import uuid
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    # The request itself is malformed (bad schema / unknown partition spec).
    VALIDATION = "VALIDATION"
    # Optimistic-concurrency conflict: base snapshot is stale.
    CONFLICT_STALE_SNAPSHOT = "CONFLICT_STALE_SNAPSHOT"
    # Overwrite overlaps partitions changed since the request's base snapshot.
    CONFLICT_OVERLAPPING_PARTITION = "CONFLICT_OVERLAPPING_PARTITION"
    # Retry budget exhausted while trying to rebase a contended append.
    CONFLICT_RETRY_EXHAUSTED = "CONFLICT_RETRY_EXHAUSTED"
    # A data file failed to stage (corrupt / unreadable / wrong schema).
    STAGING_FAILED = "STAGING_FAILED"
    # The server cannot decide (unexpected internal error / unknown state).
    INDETERMINATE = "INDETERMINATE"
    # Referenced entity does not exist.
    NOT_FOUND = "NOT_FOUND"


# Categories that the caller may safely retry after rebasing.
RETRYABLE_CATEGORIES = {
    ErrorCategory.CONFLICT_STALE_SNAPSHOT,
}


class ServiceError(Exception):
    """Domain error carrying a stable category and structured details."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        self.request_id = request_id or new_request_id()

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.category.value,
            "message": self.message,
            "request_id": self.request_id,
            "details": self.details,
        }


def new_request_id() -> str:
    """Short, sortable request identifier used in every diagnostic line."""
    return "req-" + uuid.uuid4().hex[:12]


# Keys whose *values* must never appear in logs / diagnostics.
_SENSITIVE_KEYS = {"secret", "token", "password", "email", "user", "owner"}


def redact(value: Any, *, key_hint: str | None = None) -> Any:
    """Return a log-safe copy of ``value``.

    - Sensitive scalar values are replaced by a stable hash prefix so two
      redacted logs can still be correlated without revealing the value.
    - Containers are walked recursively.
    """
    if isinstance(value, dict):
        return {k: redact(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key_hint=key_hint) for v in value]
    if key_hint is not None and key_hint.lower() in _SENSITIVE_KEYS:
        if isinstance(value, str) and value:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
            return f"<redacted:{digest}>"
    return value


def fingerprint(value: Any) -> str:
    """Stable short fingerprint for a file/value, safe to print."""
    if isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = repr(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]
