"""Error taxonomy.

Every failure raised by the format adapter or execution kernel carries a
stable ``code`` so tests can assert on the *failure category* (not just on a
string), plus a structured ``detail`` with the location information needed to
explain the failure: record index, list path, page index and slot-within-page
where applicable.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    # --- schema admission -------------------------------------------------
    UNSUPPORTED_LOGICAL_TYPE = "UNSUPPORTED_LOGICAL_TYPE"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    LEGACY_LIST_LAYOUT = "LEGACY_LIST_LAYOUT"
    EMPTY_STRUCT = "EMPTY_STRUCT"
    # --- value / schema conformance --------------------------------------
    VALUE_TYPE_MISMATCH = "VALUE_TYPE_MISMATCH"
    VALUE_OUT_OF_RANGE = "VALUE_OUT_OF_RANGE"
    NULL_IN_REQUIRED = "NULL_IN_REQUIRED"
    # --- round-trip / alignment ------------------------------------------
    COLUMN_RECORD_BOUNDARY_MISMATCH = "COLUMN_RECORD_BOUNDARY_MISMATCH"
    STRUCT_CHILD_PRESENCE_MISMATCH = "STRUCT_CHILD_PRESENCE_MISMATCH"
    ROUNDTRIP_MISMATCH = "ROUNDTRIP_MISMATCH"
    # --- page integrity ---------------------------------------------------
    PAGE_TRUNCATES_RECORD = "PAGE_TRUNCATES_RECORD"
    PAGE_INVARIANT_VIOLATION = "PAGE_INVARIANT_VIOLATION"
    # --- differential check ----------------------------------------------
    REFERENCE_MISMATCH = "REFERENCE_MISMATCH"
    # --- request shape ----------------------------------------------------
    INVALID_REQUEST = "INVALID_REQUEST"


# Levels let the API separate hard failures from uncertain conclusions.
class Severity(str, Enum):
    FATAL = "FATAL"          # the core refused the request / invariant broken
    UNCERTAIN = "UNCERTAIN"  # result exists but a reference could not confirm it
    INFO = "INFO"            # documented dialect behaviour, not a defect


@dataclass
class StructuredError(Exception):
    code: ErrorCode
    message: str
    location: dict[str, Any] = field(default_factory=dict)
    severity: Severity = Severity.FATAL

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.code.value}: {self.message} @ {self.location or '<root>'}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "severity": self.severity.value,
            "location": self.location,
        }


def error(code: ErrorCode, message: str, **location: Any) -> StructuredError:
    return StructuredError(code=code, message=message, location=location)


def errors_to_dicts(errs: list[StructuredError]) -> list[dict[str, Any]]:
    return [asdict(e) if False else e.to_dict() for e in errs]
