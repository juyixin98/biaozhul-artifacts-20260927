"""Error taxonomy shared across the service.

Every externally visible failure gets a stable ``code`` so that clients and
tests can assert the *failure class*, not just HTTP status.
"""
from __future__ import annotations


class ServiceError(Exception):
    """Base class for expected, explainable service failures."""

    http_status: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class EmptyQueryError(ServiceError):
    http_status = 400
    code = "empty_query"


class QueryTooLongError(ServiceError):
    http_status = 413
    code = "query_too_long"


class TooManyTokensError(ServiceError):
    http_status = 413
    code = "too_many_tokens"


class UnsupportedCharacterError(ServiceError):
    http_status = 422
    code = "unsupported_character"

    def __init__(self, message: str, *, char: str | None = None, position: int | None = None):
        super().__init__(message, details={"char": char, "position": position})


class VersionNotFoundError(ServiceError):
    http_status = 404
    code = "version_not_found"


class InvalidParameterError(ServiceError):
    http_status = 422
    code = "invalid_parameter"
