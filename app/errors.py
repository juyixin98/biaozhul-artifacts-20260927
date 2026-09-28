"""Failure taxonomy.

Every failure the service can deliberately produce is an :class:`AppError`
subclass carrying a stable ``code`` string and a ``category``.  The HTTP layer
maps categories to status codes; tests assert on the exact ``code`` rather than
on message wording.

Categories
----------
INPUT          caller supplied malformed data (400).  Nothing was executed.
STATE_CONFLICT the request referenced a stale/conflicting version or a plan
               already applied (409).
NOT_FOUND      referenced source/ruleset/plan does not exist (404).
RESOURCE       a configured limit was reached before any result was produced
               (413/422 -> we use 422 "unprocessable" is misleading; the API
               layer maps this to 413).
COMPUTATION    the engine/template phase failed on otherwise valid input,
               e.g. a pattern RE2 rejects at runtime or a required capture is
               missing (422).
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all structured service errors."""

    category: str = "COMPUTATION"
    code: str = "internal_error"
    http_status: int = 422

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details: dict[str, Any] = {k: v for k, v in details.items() if v is not None}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": self.code,
            "category": self.category,
            "message": self.message,
        }
        if self.details:
            d["details"] = self.details
        return d


# --------------------------------------------------------------------------- #
# INPUT (400)
# --------------------------------------------------------------------------- #
class InputError(AppError):
    category = "INPUT"
    http_status = 400


class TextDecodeError(InputError):
    code = "text_not_utf8"


class EmptyTextError(InputError):
    code = "empty_text"


class TextTooLargeError(InputError):
    """Declared/uploaded payload exceeds the hard request size cap."""

    code = "text_too_large"


class InvalidPatternError(InputError):
    code = "invalid_pattern"


class UnsupportedSyntaxError(InputError):
    """Pattern uses a construct RE2 deliberately does not implement.

    RE2 reports these as compile failures; we classify them separately so the
    caller gets a *stable* hint (backtracking features can never work here),
    instead of the raw engine string.
    """

    code = "unsupported_syntax"


class InvalidTemplateError(InputError):
    code = "invalid_template"


class UnknownCaptureError(InputError):
    """Template names a group the pattern does not define.

    Distinct from :class:`CaptureMissingError`: this is a static shape error
    detectable when the rule is created; the other only manifests on a concrete
    match where an *optional* group did not participate.
    """

    code = "unknown_capture"


class InvalidRuleError(InputError):
    code = "invalid_rule"


class InvalidByteRangeError(InputError):
    """A range is out of bounds or does not align to a UTF-8 boundary."""

    code = "invalid_byte_range"


# --------------------------------------------------------------------------- #
# STATE CONFLICT (409)
# --------------------------------------------------------------------------- #
class StateConflictError(AppError):
    category = "STATE_CONFLICT"
    http_status = 409


class SourceVersionMismatchError(StateConflictError):
    """Plan is bound to a source digest that no longer matches the target."""

    code = "source_version_mismatch"


class AlreadyAppliedError(StateConflictError):
    code = "already_applied"


# --------------------------------------------------------------------------- #
# NOT FOUND (404)
# --------------------------------------------------------------------------- #
class NotFoundError(AppError):
    category = "NOT_FOUND"
    http_status = 404


class SourceNotFound(NotFoundError):
    code = "source_not_found"


class RulesetNotFound(NotFoundError):
    code = "ruleset_not_found"


class PlanNotFound(NotFoundError):
    code = "plan_not_found"


# --------------------------------------------------------------------------- #
# RESOURCE EXHAUSTION (413)
# --------------------------------------------------------------------------- #
class ResourceExhaustedError(AppError):
    category = "RESOURCE"
    http_status = 413


class PatternBudgetExceededError(ResourceExhaustedError):
    """RE2 rejected the pattern because ``max_mem`` / program size was hit."""

    code = "pattern_budget_exceeded"


class TextBudgetExceededError(ResourceExhaustedError):
    code = "text_budget_exceeded"


class MatchBudgetExceededError(ResourceExhaustedError):
    """Too many candidate matches / planned edits (defensive DoS cap)."""

    code = "match_budget_exceeded"


class OutputBudgetExceededError(ResourceExhaustedError):
    """The rendered output would exceed the configured size ceiling."""

    code = "output_budget_exceeded"


# --------------------------------------------------------------------------- #
# COMPUTATION FAILURE (422)
# --------------------------------------------------------------------------- #
class ComputationError(AppError):
    category = "COMPUTATION"
    http_status = 422


class CaptureMissingError(ComputationError):
    """An optional capture referenced by the template did not participate.

    This is intentionally *not* silent: silently substituting empty bytes
    would hide mis-anchored rules.  Rules may opt in via
    ``missing_capture="empty"``; the default is to fail the plan.
    """

    code = "capture_missing"


class EngineFailure(ComputationError):
    """RE2 raised at execution time for reasons other than our budgets."""

    code = "engine_failure"


__all__ = [
    "AppError",
    # input
    "InputError",
    "TextDecodeError",
    "EmptyTextError",
    "TextTooLargeError",
    "InvalidPatternError",
    "UnsupportedSyntaxError",
    "InvalidTemplateError",
    "UnknownCaptureError",
    "InvalidRuleError",
    "InvalidByteRangeError",
    # state
    "StateConflictError",
    "SourceVersionMismatchError",
    "AlreadyAppliedError",
    # not found
    "NotFoundError",
    "SourceNotFound",
    "RulesetNotFound",
    "PlanNotFound",
    # resource
    "ResourceExhaustedError",
    "PatternBudgetExceededError",
    "TextBudgetExceededError",
    "MatchBudgetExceededError",
    "OutputBudgetExceededError",
    # computation
    "ComputationError",
    "CaptureMissingError",
    "EngineFailure",
]
