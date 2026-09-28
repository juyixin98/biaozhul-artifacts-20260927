"""Shared types for the differential analysis engine.

Verdict semantics (three-valued, used everywhere consistently):

* DENY_NO_MATCH  -- default deny: no statement allowed the request.
* DENY_EXPLICIT  -- explicit refuse: a Deny statement matched; Deny overrides Allow.
* ALLOW          -- an Allow statement matched and no Deny statement matched.
* UNKNOWN        -- no Allow was proven, no Deny was proven, but at least one
                    statement was indeterminate because a condition referenced an
                    unknown value. Callers MUST NOT treat this as allow.

The pairing of an old-policy verdict and a new-policy verdict determines the
transition category. EXPANSION_* categories answer "did the accessible request
set grow?" under two notions of accessibility:

* proven access:    only ALLOW counts as accessible
* possible access:  ALLOW or UNKNOWN counts as potentially accessible
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class _UnknownValue:
    """Sentinel for an attribute whose value is not known at analysis time."""

    _instance: "_UnknownValue | None" = None

    def __new__(cls) -> "_UnknownValue":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN_VALUE"

    def __bool__(self) -> bool:  # guard against accidental truthiness-based allows
        raise TypeError("UNKNOWN_VALUE must never be coerced to bool")


UNKNOWN_VALUE = _UnknownValue()
"""Marker placed in a request's attributes.  Conditions touching it evaluate to
UNKNOWN (Kleene), never to True or False."""

# JSON form used when requests/evidence cross a serialization boundary.
UNKNOWN_JSON_TAG = {"__unknown__": True}


class Verdict(str, enum.Enum):
    ALLOW = "ALLOW"
    DENY_EXPLICIT = "DENY_EXPLICIT"
    DENY_NO_MATCH = "DENY_NO_MATCH"
    UNKNOWN = "UNKNOWN"

    @property
    def is_proven_allow(self) -> bool:
        return self is Verdict.ALLOW

    @property
    def is_possible_allow(self) -> bool:
        return self in (Verdict.ALLOW, Verdict.UNKNOWN)

    @property
    def is_deny(self) -> bool:
        return self in (Verdict.DENY_EXPLICIT, Verdict.DENY_NO_MATCH)


class Category(str, enum.Enum):
    """Transition category for a single concrete request."""

    EXPANSION_PROVEN = "EXPANSION_PROVEN"          # not allowed before -> ALLOW now
    EXPANSION_POSSIBLE = "EXPANSION_POSSIBLE"      # fully denied before -> UNKNOWN now
    RESOLVED_UNCERTAINTY = "RESOLVED_UNCERTAINTY"  # UNKNOWN before -> ALLOW now (still an expansion of proven access)
    REDUCED_POSSIBLE = "REDUCED_POSSIBLE"          # UNKNOWN before -> fully denied now
    CONTRACTION = "CONTRACTION"                    # ALLOW before -> not allowed now
    DENY_TIGHTENED = "DENY_TIGHTENED"             # default deny before -> explicit deny now
    DENY_RELAXED = "DENY_RELAXED"                 # explicit deny before -> default deny now
    UNCHANGED = "UNCHANGED"


# (old, new) -> Category ; the three diagonal-uncertainty transitions are UNCHANGED.
TRANSITIONS: dict[tuple[Verdict, Verdict], Category] = {
    (Verdict.DENY_NO_MATCH, Verdict.ALLOW): Category.EXPANSION_PROVEN,
    (Verdict.DENY_EXPLICIT, Verdict.ALLOW): Category.EXPANSION_PROVEN,
    (Verdict.DENY_NO_MATCH, Verdict.UNKNOWN): Category.EXPANSION_POSSIBLE,
    (Verdict.DENY_EXPLICIT, Verdict.UNKNOWN): Category.EXPANSION_POSSIBLE,
    (Verdict.UNKNOWN, Verdict.ALLOW): Category.RESOLVED_UNCERTAINTY,
    (Verdict.UNKNOWN, Verdict.DENY_NO_MATCH): Category.REDUCED_POSSIBLE,
    (Verdict.UNKNOWN, Verdict.DENY_EXPLICIT): Category.REDUCED_POSSIBLE,
    (Verdict.ALLOW, Verdict.DENY_NO_MATCH): Category.CONTRACTION,
    (Verdict.ALLOW, Verdict.DENY_EXPLICIT): Category.CONTRACTION,
    (Verdict.DENY_NO_MATCH, Verdict.DENY_EXPLICIT): Category.DENY_TIGHTENED,
    (Verdict.DENY_EXPLICIT, Verdict.DENY_NO_MATCH): Category.DENY_RELAXED,
    (Verdict.ALLOW, Verdict.ALLOW): Category.UNCHANGED,
    (Verdict.DENY_NO_MATCH, Verdict.DENY_NO_MATCH): Category.UNCHANGED,
    (Verdict.DENY_EXPLICIT, Verdict.DENY_EXPLICIT): Category.UNCHANGED,
    (Verdict.UNKNOWN, Verdict.UNKNOWN): Category.UNCHANGED,
}

# Proven expansion: a request that used to be fully denied is now ALLOW.
PROVEN_EXPANSION_CATEGORIES = frozenset(
    {Category.EXPANSION_PROVEN, Category.RESOLVED_UNCERTAINTY}
)
# Possible expansion adds: fully-denied -> UNKNOWN (access might be granted).
POSSIBLE_EXPANSION_CATEGORIES = PROVEN_EXPANSION_CATEGORIES | {Category.EXPANSION_POSSIBLE}
# Backwards-compatible alias.
EXPANSION_CATEGORIES = POSSIBLE_EXPANSION_CATEGORIES
CONTRACTION_CATEGORIES = frozenset(
    {Category.CONTRACTION, Category.REDUCED_POSSIBLE}
)


class Failure(str, enum.Enum):
    """Failure classes. A run with a failure has no verdict result."""

    PARSE_ERROR = "PARSE_ERROR"            # a policy document could not be parsed
    SPACE_LIMIT_EXCEEDED = "SPACE_LIMIT_EXCEEDED"  # bounded space is too large to exhaust
    EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"  # re-checking a witness under a policy disagrees
    BAD_SIGNATURE = "BAD_SIGNATURE"        # signed evidence failed verification
    NOT_FOUND = "NOT_FOUND"                # referenced run/evidence id unknown
    INVALID_REQUEST = "INVALID_REQUEST"    # malformed API request


@dataclass(frozen=True)
class Witness:
    """A concrete request that witnesses one transition, with both real verdicts."""

    request: dict[str, Any]
    old_verdict: Verdict
    new_verdict: Verdict
    category: Category
    old_trace: list[dict[str, Any]]
    new_trace: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "old_verdict": self.old_verdict.value,
            "new_verdict": self.new_verdict.value,
            "category": self.category.value,
            "old_trace": self.old_trace,
            "new_trace": self.new_trace,
        }


@dataclass
class RunResult:
    run_id: str
    old_version_id: str
    new_version_id: str
    old_policy_hash: str
    new_policy_hash: str
    space_size: int
    space_basis: dict[str, Any]
    counts: dict[str, int]
    witnesses: list[Witness] = field(default_factory=list)
    witness_limit: int = 0
    witnesses_truncated: bool = False
    expands: bool = False
    possibly_expands: bool = False
    contracts: bool = False
    created_at: str = ""
    signature: str | None = None

    def summary(self) -> dict[str, Any]:
        """Serialization used by the API, CLI and the signed payload."""
        return {
            "run_id": self.run_id,
            "old_version_id": self.old_version_id,
            "new_version_id": self.new_version_id,
            "old_policy_hash": self.old_policy_hash,
            "new_policy_hash": self.new_policy_hash,
            "space_size": self.space_size,
            "space_basis": self.space_basis,
            "counts": dict(self.counts),
            "witnesses": [w.to_dict() for w in self.witnesses],
            "witness_limit": self.witness_limit,
            "witnesses_truncated": self.witnesses_truncated,
            "expands": self.expands,
            "possibly_expands": self.possibly_expands,
            "contracts": self.contracts,
            "created_at": self.created_at,
            "signature": self.signature,
        }
