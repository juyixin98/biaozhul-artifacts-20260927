"""Error contract shared by every module.

All failures carry a stable machine-readable ``code`` and one of five
top-level ``category`` values, so callers (and tests) can distinguish:

* ``INPUT``               - malformed / unbindable user input
* ``STATE_CONFLICT``      - structurally valid input that conflicts with trusted state
* ``RESOURCE``            - a configured resource/size limit or storage failure
* ``COMPUTATION``         - cryptographic / threshold verification failure
* ``TRUST``               - the trust period has elapsed; a new checkpoint is required

``INTERNAL`` is reserved for bugs in our own code and must never be the
expected result of feeding external input.
"""

from __future__ import annotations

import enum
from typing import Any, Dict, Optional


class Category(str, enum.Enum):
    INPUT = "input_error"
    STATE_CONFLICT = "state_conflict"
    RESOURCE = "resource_exhausted"
    COMPUTATION = "computation_failure"
    TRUST = "trust_expired"
    INTERNAL = "internal"


class Code(str, enum.Enum):
    # --- input errors -------------------------------------------------------
    MALFORMED_HEADER = "MALFORMED_HEADER"
    MALFORMED_CERTIFICATE = "MALFORMED_CERTIFICATE"
    MALFORMED_COMMITTEE = "MALFORMED_COMMITTEE"
    MALFORMED_CHECKPOINT = "MALFORMED_CHECKPOINT"
    CERT_BIND_MISMATCH = "CERT_BIND_MISMATCH"
    HEADER_FUTURE = "HEADER_FUTURE"
    COMMITTEE_NOT_PROVIDED = "COMMITTEE_NOT_PROVIDED"
    BATCH_DUPLICATE_HEADER = "BATCH_DUPLICATE_HEADER"
    ROTATION_MISSING_COMMITTEE = "ROTATION_MISSING_COMMITTEE"
    ROTATION_COMMITMENT_MISMATCH = "ROTATION_COMMITMENT_MISMATCH"
    # --- state conflicts ----------------------------------------------------
    NOT_INITIALIZED = "NOT_INITIALIZED"
    CHECKPOINT_CONFLICT = "CHECKPOINT_CONFLICT"
    UNTRUSTED_BRANCH = "UNTRUSTED_BRANCH"
    STALE_ROUND = "STALE_ROUND"
    CONFLICT_EQUIVOCATION = "CONFLICT_EQUIVOCATION"
    ALREADY_KNOWN = "ALREADY_KNOWN"
    HEADER_BACKDATED = "HEADER_BACKDATED"
    # --- resource exhaustion ------------------------------------------------
    BATCH_TOO_LARGE = "BATCH_TOO_LARGE"
    COMMITTEE_TOO_LARGE = "COMMITTEE_TOO_LARGE"
    STORAGE_FAILURE = "STORAGE_FAILURE"
    # --- cryptographic / threshold computation ------------------------------
    SIGNER_UNKNOWN = "SIGNER_UNKNOWN"
    CRYPTO_BAD_SIGNATURE = "CRYPTO_BAD_SIGNATURE"
    INSUFFICIENT_WEIGHT = "INSUFFICIENT_WEIGHT"
    # --- trust boundary -----------------------------------------------------
    TRUST_EXPIRED = "TRUST_EXPIRED"
    # --- ours ---------------------------------------------------------------
    INTERNAL = "INTERNAL"


_CODE_CATEGORY: Dict[Code, Category] = {
    Code.MALFORMED_HEADER: Category.INPUT,
    Code.MALFORMED_CERTIFICATE: Category.INPUT,
    Code.MALFORMED_COMMITTEE: Category.INPUT,
    Code.MALFORMED_CHECKPOINT: Category.INPUT,
    Code.CERT_BIND_MISMATCH: Category.INPUT,
    Code.HEADER_FUTURE: Category.INPUT,
    Code.COMMITTEE_NOT_PROVIDED: Category.INPUT,
    Code.BATCH_DUPLICATE_HEADER: Category.INPUT,
    Code.ROTATION_MISSING_COMMITTEE: Category.INPUT,
    Code.ROTATION_COMMITMENT_MISMATCH: Category.INPUT,
    Code.NOT_INITIALIZED: Category.STATE_CONFLICT,
    Code.CHECKPOINT_CONFLICT: Category.STATE_CONFLICT,
    Code.UNTRUSTED_BRANCH: Category.STATE_CONFLICT,
    Code.STALE_ROUND: Category.STATE_CONFLICT,
    Code.CONFLICT_EQUIVOCATION: Category.STATE_CONFLICT,
    Code.ALREADY_KNOWN: Category.STATE_CONFLICT,
    Code.HEADER_BACKDATED: Category.STATE_CONFLICT,
    Code.BATCH_TOO_LARGE: Category.RESOURCE,
    Code.COMMITTEE_TOO_LARGE: Category.RESOURCE,
    Code.STORAGE_FAILURE: Category.RESOURCE,
    Code.SIGNER_UNKNOWN: Category.COMPUTATION,
    Code.CRYPTO_BAD_SIGNATURE: Category.COMPUTATION,
    Code.INSUFFICIENT_WEIGHT: Category.COMPUTATION,
    Code.TRUST_EXPIRED: Category.TRUST,
    Code.INTERNAL: Category.INTERNAL,
}

# HTTP status mapping used by the service layer.
HTTP_STATUS: Dict[Category, int] = {
    Category.INPUT: 400,
    Category.STATE_CONFLICT: 409,
    Category.COMPUTATION: 422,
    Category.RESOURCE: 413,
    Category.TRUST: 410,
    Category.INTERNAL: 500,
}


def category_of(code: Code) -> Category:
    return _CODE_CATEGORY[code]


class LightClientError(Exception):
    """Raised for every rule violation. Carries a stable code + diagnostics."""

    def __init__(
        self,
        code: Code,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
        reasons: Optional[list] = None,
        trace: Optional[list] = None,
    ):
        super().__init__(message)
        self.code = code
        self.category = category_of(code)
        self.message = message
        self.details: Dict[str, Any] = dict(details or {})
        self.reasons: list = list(reasons or [])
        self.trace: list = list(trace or [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "category": self.category.value,
            "message": self.message,
            "details": self.details,
            "reasons": self.reasons,
        }


class StorageError(LightClientError):
    """SQLite / persistence failure -> RESOURCE (caller may retry elsewhere)."""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None):
        super().__init__(Code.STORAGE_FAILURE, message, details=details)
