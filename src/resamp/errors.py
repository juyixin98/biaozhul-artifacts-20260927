"""Error contract shared across DSP core, services and the HTTP layer.

Every domain error carries a stable ``category`` string and an ``error_code``
so that callers (and test logs) can distinguish the four failure classes the
project requires:

* ``invalid_input``     — malformed/non-finite input, unsupported media
* ``state_conflict``    — illegal job state transition (double flush, etc.)
* ``resource_exhausted``— configured memory/filter limits reached
* ``computation``       — arithmetic failure (non-finite output / overflow)

``NotFound`` is a specialized invalid-input error with its own HTTP status
(404); it keeps ``category == "invalid_input"`` so the four-class contract is
preserved.
"""
from __future__ import annotations


class ResampError(Exception):
    """Base class for all domain errors."""

    category: str = "error"
    error_code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "error": self.error_code,
            "category": self.category,
            "message": self.message,
            "details": self.details,
        }


class InvalidInputError(ResampError):
    category = "invalid_input"
    error_code = "invalid_input"
    http_status = 400


class NotFoundError(InvalidInputError):
    error_code = "not_found"
    http_status = 404


class StateConflictError(ResampError):
    category = "state_conflict"
    error_code = "state_conflict"
    http_status = 409


class ResourceExhaustedError(ResampError):
    category = "resource_exhausted"
    error_code = "resource_exhausted"
    http_status = 413


class ComputationError(ResampError):
    category = "computation"
    error_code = "computation_failed"
    http_status = 422
