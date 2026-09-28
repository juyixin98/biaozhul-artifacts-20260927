"""Error taxonomy for the audit backend.

Every failure crossing a module boundary is an ``AuditError`` carrying a
``category`` so callers can distinguish:

- ``input``       -> malformed client-supplied data (HTTP 400)
- ``state``       -> conflict with persisted state (HTTP 409 / 404)
- ``resource``    -> a configured limit was exceeded (HTTP 429)
- ``computation`` -> an internal invariant failed while keying/auditing (HTTP 500)
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    INPUT = "input"
    STATE = "state"
    RESOURCE = "resource"
    COMPUTATION = "computation"


class AuditError(Exception):
    """Base class for all errors raised across module boundaries."""

    category: ErrorCategory = ErrorCategory.COMPUTATION
    http_status: int = 500

    def __init__(self, message: str, *, code: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "category": self.category.value,
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        }


class InputError(AuditError):
    """Client-supplied policy or evidence is malformed."""

    category = ErrorCategory.INPUT
    http_status = 400


class StateConflictError(AuditError):
    """Request conflicts with persisted state (duplicate run, finalized run)."""

    category = ErrorCategory.STATE
    http_status = 409


class NotFoundError(StateConflictError):
    """Referenced run does not exist."""

    http_status = 404


class ResourceExhaustedError(AuditError):
    """A configured limit (requests per run, findings per run) was exceeded."""

    category = ErrorCategory.RESOURCE
    http_status = 429


class ComputationError(AuditError):
    """An internal invariant failed while deriving keys or auditing."""

    category = ErrorCategory.COMPUTATION
    http_status = 500
