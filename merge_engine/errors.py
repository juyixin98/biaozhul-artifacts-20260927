"""Error contract.

Every failure raised by the engine is a :class:`MergeError` carrying:

* ``category`` - one of the four externally distinguishable categories:

    INPUT_ERROR       the request itself is malformed (bad shape, bad contract,
                      source-internal conflicts). Re-submitting the same
                      request cannot succeed.
    STATE_CONFLICT    the request is well-formed but the target state does not
                      satisfy the preconditions (duplicate keys in target,
                      concurrent modification between snapshot and execution,
                      constraint violation during execution).
    RESOURCE_EXHAUSTED a hard resource boundary was hit: configured row limit,
                      SQLite lock/busy, or an injected commit failure.
    COMPUTATION_FAILURE a WHEN condition / value expression raised while being
                      evaluated (type error, division by zero, ...).

* ``code``    - stable machine-readable string (see the ``*_CODE`` constants),
                safe to switch on in tests and clients.
* ``details`` - structured JSON-able payload with offending keys / rows.

The category/code split is the data-and-error contract between modules:
adapter -> INPUT_ERROR, planner/snapshot -> INPUT_ERROR/STATE_CONFLICT/
COMPUTATION_FAILURE, executor -> STATE_CONFLICT/RESOURCE_EXHAUSTED/
COMPUTATION_FAILURE.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class Category(str, Enum):
    INPUT_ERROR = "INPUT_ERROR"
    STATE_CONFLICT = "STATE_CONFLICT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    COMPUTATION_FAILURE = "COMPUTATION_FAILURE"


# Stable codes. Categories intentionally encoded in the code name as well so
# log greps do not need the surrounding envelope.
# --- INPUT_ERROR -----------------------------------------------------------
SPEC_INVALID_CODE = "INPUT_INVALID_SPEC"
SOURCE_SCHEMA_CODE = "INPUT_SOURCE_SCHEMA"
SOURCE_DUPLICATE_KEY_CODE = "INPUT_SOURCE_DUPLICATE_KEY"
UNSUPPORTED_VALUE_CODE = "INPUT_UNSUPPORTED_VALUE"
ROW_LIMIT_CODE = "INPUT_ROW_LIMIT_EXCEEDED"
# --- STATE_CONFLICT --------------------------------------------------------
TARGET_DUPLICATE_KEY_CODE = "STATE_TARGET_DUPLICATE_KEY"
TARGET_TABLE_MISSING_CODE = "STATE_TARGET_TABLE_MISSING"
SNAPSHOT_STALE_CODE = "STATE_SNAPSHOT_STALE"
CONSTRAINT_VIOLATION_CODE = "STATE_CONSTRAINT_VIOLATION"
# --- RESOURCE_EXHAUSTED ----------------------------------------------------
DB_LOCKED_CODE = "RESOURCE_DB_LOCKED"
COMMIT_FAILED_CODE = "RESOURCE_COMMIT_FAILED"
# --- COMPUTATION_FAILURE ---------------------------------------------------
PREDICATE_FAILED_CODE = "COMPUTE_PREDICATE_EVALUATION"


class MergeError(Exception):
    """Base class for every engine failure with a stable category/code."""

    category: Category = Category.COMPUTATION_FAILURE

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details or extra or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class InputError(MergeError):
    category = Category.INPUT_ERROR


class StateConflictError(MergeError):
    category = Category.STATE_CONFLICT


class ResourceExhaustedError(MergeError):
    category = Category.RESOURCE_EXHAUSTED


class ComputationFailureError(MergeError):
    category = Category.COMPUTATION_FAILURE
