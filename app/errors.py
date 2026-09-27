"""Stable failure categories shared by parsing, kernel and API.

Every rejected request ends in exactly one of these codes, carried verbatim in
the JSON ``error.code`` field and in structured logs. They are intentionally
narrow so tests can assert the *kind* of failure, not just that one happened.
"""
from __future__ import annotations


class R128Error(Exception):
    """Base class. ``code`` is the stable, machine-readable category."""

    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class UnsupportedFormatError(R128Error):
    """Container/sample format we explicitly do not parse (e.g. not PCM)."""

    code = "UNSUPPORTED_FORMAT"
    http_status = 415


class InvalidMediaError(R128Error):
    """Truncated/corrupt payload, bad bit depth, inconsistent framing."""

    code = "INVALID_MEDIA"
    http_status = 400


class UnsupportedSampleRateError(R128Error):
    """Anything that is not the normative 48 kHz is rejected, not resampled."""

    code = "UNSUPPORTED_SAMPLE_RATE"
    http_status = 422


class UnsupportedLayoutError(R128Error):
    """Channel count with no known layout, or an unknown explicit role."""

    code = "UNSUPPORTED_LAYOUT"
    http_status = 422


class InvalidLayoutError(R128Error):
    """Explicit roles whose length does not match the channel count."""

    code = "INVALID_LAYOUT"
    http_status = 400


class JobError(R128Error):
    """Unknown/closed job, appending after finalize, size limit, bad state."""

    code = "JOB_ERROR"
    http_status = 409
