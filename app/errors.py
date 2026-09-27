"""Domain error taxonomy.

Every error the service can *decide* on maps to a subclass of :class:`DomainError`
carrying a stable machine-readable ``code``, an HTTP status and the diagnostic
decision category ("accept" / "reject" / "inconclusive").

Unexpected internal failures are wrapped as :class:`InternalError` and reported
as "inconclusive" in diagnostics: the service cannot prove the request was
valid or that the match result is complete.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class DomainError(Exception):
    """Base class for errors the service has an explicit policy for."""

    code: str = "domain_error"
    http_status: int = 400
    decision: str = "reject"  # one of: accept / reject / inconclusive

    def __init__(
        self,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
        code: Optional[str] = None,
        http_status: Optional[int] = None,
        decision: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = dict(details or {})
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if decision is not None:
            self.decision = decision

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "decision": self.decision,
                "details": self.details,
            }
        }


# --- request-level rejections -------------------------------------------------


class EmptyPatternError(DomainError):
    code = "empty_pattern"
    http_status = 422
    decision = "reject"


class DuplicatePatternError(DomainError):
    code = "duplicate_pattern"
    http_status = 422
    decision = "reject"


class EncodingError(DomainError):
    """Payload bytes are not valid for the declared text encoding."""

    code = "invalid_encoding"
    http_status = 422
    decision = "reject"


class UnsupportedEncodingError(DomainError):
    code = "unsupported_encoding"
    http_status = 422
    decision = "reject"


class InvalidBase64Error(DomainError):
    code = "invalid_base64"
    http_status = 422
    decision = "reject"


class InvalidCursorError(DomainError):
    code = "invalid_cursor"
    http_status = 400
    decision = "reject"


class LimitOutOfRangeError(DomainError):
    code = "limit_out_of_range"
    http_status = 422
    decision = "reject"


class StaleCursorError(DomainError):
    """Cursor belongs to an older state epoch (e.g. after an explicit reset)."""

    code = "stale_cursor"
    http_status = 409
    decision = "reject"


class VersionMismatchError(DomainError):
    """Chunk references a different pattern version than the open scan."""

    code = "version_mismatch"
    http_status = 409
    decision = "reject"


# --- not-found ----------------------------------------------------------------


class VersionNotFoundError(DomainError):
    code = "version_not_found"
    http_status = 404
    decision = "reject"


class ScanNotFoundError(DomainError):
    code = "scan_not_found"
    http_status = 404
    decision = "reject"


# --- state / lifecycle ---------------------------------------------------------


class ScanClosedError(DomainError):
    code = "scan_closed"
    http_status = 409
    decision = "reject"


# --- inconclusive: the service cannot make a guaranteed decision --------------


class InternalError(DomainError):
    """Defensive 500. Decision is *inconclusive*, never silently accepted."""

    code = "internal_error"
    http_status = 500
    decision = "inconclusive"
