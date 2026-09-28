"""Failure categories shared by the core, the job store and the API.

Every abnormal outcome carries one of these categories so that tests and
clients can assert on the *kind* of failure instead of a generic error.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class FailureCategory(str, Enum):
    # input / compatibility
    INPUT_ERROR = "INPUT_ERROR"
    CODEC_MISMATCH = "CODEC_MISMATCH"
    PARAM_MISMATCH = "PARAM_MISMATCH"
    TIMEBASE_MISMATCH = "TIMEBASE_MISMATCH"
    # sample selection
    EMPTY_WINDOW = "EMPTY_WINDOW"
    KEYFRAME_BOUNDARY = "KEYFRAME_BOUNDARY"
    MISSING_REFERENCE = "MISSING_REFERENCE"
    # container / plan validation
    NEGATIVE_DTS = "NEGATIVE_DTS"
    DTS_NOT_MONOTONIC = "DTS_NOT_MONOTONIC"
    PTS_BEFORE_DTS = "PTS_BEFORE_DTS"
    AUDIO_PRIMING_RANGE = "AUDIO_PRIMING_RANGE"
    COVERAGE_GAP = "COVERAGE_GAP"
    PADDING_MISUSE = "PADDING_MISUSE"
    CONTAINER_VIOLATION = "CONTAINER_VIOLATION"
    # catch-all, never mapped to success
    INTERNAL_ERROR = "INTERNAL_ERROR"


class PlannerError(Exception):
    """A structured, categorised failure raised by the planning core."""

    def __init__(self, category: FailureCategory, detail: str,
                 context: dict[str, Any] | None = None) -> None:
        super().__init__(f"{category.value}: {detail}")
        self.category = category
        self.detail = detail
        self.context = context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "detail": self.detail,
            "context": self.context,
        }
