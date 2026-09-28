"""Typed error taxonomy.

Every failure carries a stable machine-readable ``code`` so that CLI output,
HTTP responses and log lines can classify *why* something failed instead of
surfacing a free-form traceback.
"""

from __future__ import annotations


class SecretscanError(Exception):
    """Base class for all expected errors raised by this package."""

    code = "internal_error"
    retriable = False

    def __init__(self, message: str, *, code: str | None = None, retriable: bool | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retriable is not None:
            self.retriable = retriable

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retriable": self.retriable}


class ConfigError(SecretscanError):
    """Rule / configuration file is missing, malformed or fails validation."""

    code = "config_invalid"


class RootError(SecretscanError):
    """The snapshot root supplied to a scan is missing or not a directory."""

    code = "root_invalid"


class NotFoundError(SecretscanError):
    """A referenced scan / project / candidate does not exist."""

    code = "not_found"


class ValidationError(SecretscanError):
    """Caller-supplied parameters are semantically invalid."""

    code = "validation_error"


class StateConflictError(SecretscanError):
    """Requested state transition is not allowed for the current record."""

    code = "state_conflict"


class StorageError(SecretscanError):
    """Persistent state could not be read or written."""

    code = "storage_failure"
    retriable = True
