"""Error taxonomy and cross-module error contract.

Four top-level categories are deliberately kept disjoint so that callers
(API layer, replay engine, tests) can distinguish *what kind* of failure
happened instead of only knowing "it failed":

    INPUT     - malformed data supplied by the caller; never a state change
    STATE     - the data was well-formed but conflicts with trusted state,
                or fails a protocol authorization rule; never a state change
    RESOURCE  - a configured resource bound was exceeded; never a state change
    COMPUTE   - an internal cryptographic/runtime computation failed;
                treated as safety-fatal: state is never mutated on this path

Every error carries a stable machine-readable ``code``, a human ``reason``
and the optional structured ``detail`` dict.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    INPUT = "input"
    STATE = "state"
    RESOURCE = "resource"
    COMPUTE = "compute"


# Stable wire codes. The category mapping is the single source of truth.
class ErrorCode(str, Enum):
    # ---- input errors ------------------------------------------------
    INPUT_MALFORMED = "INPUT_MALFORMED"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    CHECKPOINT_SIGNATURE_INVALID = "CHECKPOINT_SIGNATURE_INVALID"
    CHAIN_MISMATCH = "CHAIN_MISMATCH"

    # ---- state / authorization conflicts -----------------------------
    PARENT_UNKNOWN = "PARENT_UNKNOWN"
    UNTRUSTED_BRANCH = "UNTRUSTED_BRANCH"
    ROOT_UPDATE_FORBIDDEN = "ROOT_UPDATE_FORBIDDEN"
    CONFLICTING_HEADER = "CONFLICTING_HEADER"
    ROUND_NOT_MONOTONIC = "ROUND_NOT_MONOTONIC"
    TIMESTAMP_NOT_MONOTONIC = "TIMESTAMP_NOT_MONOTONIC"
    WEIGHT_BELOW_QUORUM = "WEIGHT_BELOW_QUORUM"
    STALE_COMMITTEE = "STALE_COMMITTEE"
    COMMITTEE_UNKNOWN = "COMMITTEE_UNKNOWN"
    COMMITTEE_BAD_TRANSITION = "COMMITTEE_BAD_TRANSITION"
    NEED_CHECKPOINT = "NEED_CHECKPOINT"
    NOT_INITIALIZED = "NOT_INITIALIZED"
    ALREADY_INITIALIZED = "ALREADY_INITIALIZED"

    # ---- resource exhaustion -----------------------------------------
    RESOURCE_LIMIT = "RESOURCE_LIMIT"

    # ---- compute failure ---------------------------------------------
    COMPUTE_FAILED = "COMPUTE_FAILED"


_CODE_CATEGORY: dict[ErrorCode, ErrorCategory] = {
    ErrorCode.INPUT_MALFORMED: ErrorCategory.INPUT,
    ErrorCode.SIGNATURE_INVALID: ErrorCategory.INPUT,
    ErrorCode.CHECKPOINT_SIGNATURE_INVALID: ErrorCategory.INPUT,
    ErrorCode.CHAIN_MISMATCH: ErrorCategory.INPUT,
    ErrorCode.PARENT_UNKNOWN: ErrorCategory.STATE,
    ErrorCode.UNTRUSTED_BRANCH: ErrorCategory.STATE,
    ErrorCode.ROOT_UPDATE_FORBIDDEN: ErrorCategory.STATE,
    ErrorCode.CONFLICTING_HEADER: ErrorCategory.STATE,
    ErrorCode.ROUND_NOT_MONOTONIC: ErrorCategory.STATE,
    ErrorCode.TIMESTAMP_NOT_MONOTONIC: ErrorCategory.STATE,
    ErrorCode.WEIGHT_BELOW_QUORUM: ErrorCategory.STATE,
    ErrorCode.STALE_COMMITTEE: ErrorCategory.STATE,
    ErrorCode.COMMITTEE_UNKNOWN: ErrorCategory.STATE,
    ErrorCode.COMMITTEE_BAD_TRANSITION: ErrorCategory.STATE,
    ErrorCode.NEED_CHECKPOINT: ErrorCategory.STATE,
    ErrorCode.NOT_INITIALIZED: ErrorCategory.STATE,
    ErrorCode.ALREADY_INITIALIZED: ErrorCategory.STATE,
    ErrorCode.RESOURCE_LIMIT: ErrorCategory.RESOURCE,
    ErrorCode.COMPUTE_FAILED: ErrorCategory.COMPUTE,
}

# HTTP status used by the service layer for each category.
CATEGORY_HTTP_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.INPUT: 400,
    ErrorCategory.STATE: 409,
    ErrorCategory.RESOURCE: 413,
    ErrorCategory.COMPUTE: 500,
}


class LightClientError(Exception):
    """Base class for every typed error raised across the package."""

    code: ErrorCode = ErrorCode.COMPUTE_FAILED

    def __init__(
        self,
        reason: str,
        detail: dict[str, Any] | None = None,
        *,
        code: ErrorCode | None = None,
    ) -> None:
        super().__init__(reason)
        if code is not None:
            self.code = code
        self.reason = reason
        self.detail: dict[str, Any] = detail or {}

    @property
    def category(self) -> ErrorCategory:
        return _CODE_CATEGORY[self.code]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "category": self.category.value,
                "reason": self.reason,
                "detail": self.detail,
            },
        }


class InputMalformed(LightClientError):
    code = ErrorCode.INPUT_MALFORMED


class SignatureInvalid(LightClientError):
    code = ErrorCode.SIGNATURE_INVALID


class CheckpointSignatureInvalid(LightClientError):
    code = ErrorCode.CHECKPOINT_SIGNATURE_INVALID


class ChainMismatch(LightClientError):
    code = ErrorCode.CHAIN_MISMATCH


class ParentUnknown(LightClientError):
    code = ErrorCode.PARENT_UNKNOWN


class UntrustedBranch(LightClientError):
    code = ErrorCode.UNTRUSTED_BRANCH


class RootUpdateForbidden(LightClientError):
    code = ErrorCode.ROOT_UPDATE_FORBIDDEN


class ConflictingHeader(LightClientError):
    code = ErrorCode.CONFLICTING_HEADER


class RoundNotMonotonic(LightClientError):
    code = ErrorCode.ROUND_NOT_MONOTONIC


class TimestampNotMonotonic(LightClientError):
    code = ErrorCode.TIMESTAMP_NOT_MONOTONIC


class WeightBelowQuorum(LightClientError):
    code = ErrorCode.WEIGHT_BELOW_QUORUM


class StaleCommittee(LightClientError):
    code = ErrorCode.STALE_COMMITTEE


class CommitteeUnknown(LightClientError):
    code = ErrorCode.COMMITTEE_UNKNOWN


class CommitteeBadTransition(LightClientError):
    code = ErrorCode.COMMITTEE_BAD_TRANSITION


class NeedCheckpoint(LightClientError):
    code = ErrorCode.NEED_CHECKPOINT


class NotInitialized(LightClientError):
    code = ErrorCode.NOT_INITIALIZED


class AlreadyInitialized(LightClientError):
    code = ErrorCode.ALREADY_INITIALIZED


class ResourceLimit(LightClientError):
    code = ErrorCode.RESOURCE_LIMIT


class ComputeFailed(LightClientError):
    code = ErrorCode.COMPUTE_FAILED
