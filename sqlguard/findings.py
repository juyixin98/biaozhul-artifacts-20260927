"""Review result domain types shared by the kernel, the store and the API."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict


class Verdict(str, enum.Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    UNANALYZABLE = "unanalyzable"


class Severity(str, enum.Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# Stable rejection / unanalyzable category codes. These are part of the API
# contract; golden cases assert on them directly.
REJECTION_CODES = frozenset({
    "STACKED_STATEMENTS",
    "STATEMENT_NOT_ALLOWED",
    "TABLE_NOT_WHITELISTED",
    "TABLE_OP_NOT_ALLOWED",
    "COLUMN_NOT_WHITELISTED",
    "VALUE_USED_AS_IDENTIFIER",
    "IDENTIFIER_SLOT_NOT_DECLARED",
    "IDENTIFIER_SLOT_UNBOUND",
    "IDENTIFIER_NOT_ALLOWED",
    "SLOT_ROLE_MISMATCH",
    "PARAMETER_UNBOUND",
    "PARAMETER_TYPE_INVALID",
    "ARRAY_EMPTY",
    "ARRAY_TOO_LONG",
    "ARRAY_ELEMENT_INVALID",
    "LIMIT_VALUE_INVALID",
    "COLUMN_UNRESOLVED",
    "BINDING_UNUSED",          # warning, never alone rejects
})

UNANALYZABLE_CODES = frozenset({
    "LEX_ERROR",
    "PARSE_ERROR",
    "UNSUPPORTED_SYNTAX",
    "SCHEMA_UNAVAILABLE",
    "AMBIGUOUS_IDENTIFIER",
    "INTERNAL_ERROR",
})


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    span: dict | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewResult:
    verdict: str
    request_id: str
    sql_digest: str
    findings: list[Finding] = field(default_factory=list)
    bound_parameters: list[dict] = field(default_factory=list)
    identifier_bindings: list[dict] = field(default_factory=list)
    inert_occurrences: list[dict] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    basis: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "request_id": self.request_id,
            "sql_digest": self.sql_digest,
            "findings": [f.to_dict() if isinstance(f, Finding) else f
                         for f in self.findings],
            "bound_parameters": self.bound_parameters,
            "identifier_bindings": self.identifier_bindings,
            "inert_occurrences": self.inert_occurrences,
            "statements": self.statements,
            "basis": self.basis,
            "limitations": self.limitations,
            "diagnostics": self.diagnostics,
        }
