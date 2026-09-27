"""Typed failure categories used across pipelines, API and tests.

Every rejection path raises one of these so the API can report a *category*
plus a human-readable reason instead of a generic 500.
"""
from __future__ import annotations


class ClockAlignError(Exception):
    """Base class. ``code`` is a stable machine-readable failure category."""

    code = "error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class MediaError(ClockAlignError):
    code = "media_error"


class InsufficientEvidenceError(ClockAlignError):
    """Raised when a correction cannot be justified from the evidence.

    Per the acceptance rules: too few sync points, too short a span, a
    correlation peak that is not good enough, or an implausibly large fitted
    drift all lead here -- the audio is returned *uncorrected* rather than
    being warped on a guess.
    """

    code = "insufficient_evidence"


class FitError(ClockAlignError):
    code = "fit_failed"


class StorageError(ClockAlignError):
    code = "storage_error"


class ValidationError(ClockAlignError):
    code = "validation_failed"
