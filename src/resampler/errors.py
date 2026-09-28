"""Error contract shared by every module.

Five error categories are kept distinct end-to-end (HTTP status in brackets):

    InputValidationError     [400]  malformed media, bad ratio/encoding params
    StateConflictError       [409]  illegal transition (flush twice, chunk after flush, ...)
    ResourceExhaustedError   [413]  configured caps exceeded (jobs/samples/filter size)
    ComputationError         [500]  non-finite numerics or kernel-level arithmetic failure
    OutputOverflowError      [422]  output samples out of representable range (hard clip policy)

Every error carries a stable machine-readable ``code`` and a human ``message``;
optionally a ``detail`` mapping with replay-relevant fields (run_id, job_id,
sample index, chunk index, ...).
"""

from __future__ import annotations

from typing import Any


class ResamplerError(Exception):
    """Base class for all project errors."""

    category: str = "error"
    http_status: int = 400
    code: str = "error"

    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "category": self.category,
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        }


class InputValidationError(ResamplerError):
    category = "input_error"
    http_status = 400
    code = "invalid_input"


class StateConflictError(ResamplerError):
    category = "state_conflict"
    http_status = 409
    code = "state_conflict"


class ResourceExhaustedError(ResamplerError):
    category = "resource_exhausted"
    http_status = 413
    code = "resource_exhausted"


class ComputationError(ResamplerError):
    category = "computation_failure"
    http_status = 500
    code = "computation_failure"


class OutputOverflowError(ResamplerError):
    category = "output_overflow"
    http_status = 422
    code = "output_overflow"
