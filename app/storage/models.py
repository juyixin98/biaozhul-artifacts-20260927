"""Publish payload models and validation.

Validation rejects the *whole* publish on any invalid entry (fail-closed): a
dictionary is only ever published as one complete, consistent version. Each
issue carries a machine-readable ``code`` so callers and tests can assert the
failure category instead of matching prose.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..core.normalize import normalize_surface


# Failure codes (part of the public error contract).
CODE_EMPTY_BATCH = "empty_batch"
CODE_NOT_A_LIST = "not_a_list"
CODE_ENTRY_NOT_OBJECT = "entry_not_object"
CODE_SURFACE_MISSING = "surface_missing"
CODE_SURFACE_NOT_STRING = "surface_not_string"
CODE_SURFACE_EMPTY = "surface_empty"
CODE_NORMALIZED_EMPTY = "normalized_empty"
CODE_FREQUENCY_INVALID = "frequency_invalid"
CODE_COST_INVALID = "cost_invalid"
CODE_DUPLICATE_SURFACE = "duplicate_surface"


class PublishValidationError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__(f"dictionary publish rejected with {len(issues)} issue(s)")
        self.issues = issues


@dataclass(frozen=True)
class RawEntry:
    surface: str
    frequency: Optional[int] = None
    cost: Optional[float] = None


@dataclass(frozen=True)
class PreparedEntry:
    surface: str           # as published
    key: str               # normalized lookup key
    frequency: int
    cost: Optional[float]  # explicit cost, or None to derive from frequency


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):  # bool is an int subclass -- reject explicitly
        return None
    if isinstance(value, int):
        return value
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def validate_entries(payload: Any) -> list[PreparedEntry]:
    """Validate a raw publish payload into prepared entries or raise."""
    issues: list[dict[str, Any]] = []

    if not isinstance(payload, list):
        raise PublishValidationError(
            [{"index": None, "code": CODE_NOT_A_LIST, "message": "entries must be a list"}]
        )
    if len(payload) == 0:
        raise PublishValidationError(
            [{"index": None, "code": CODE_EMPTY_BATCH, "message": "refusing to publish an empty dictionary"}]
        )

    prepared: list[PreparedEntry] = []
    prepared_index: list[int] = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, dict):
            issues.append({"index": index, "code": CODE_ENTRY_NOT_OBJECT,
                           "message": "entry must be an object"})
            continue
        surface = raw.get("surface")
        if "surface" not in raw:
            issues.append({"index": index, "code": CODE_SURFACE_MISSING,
                           "message": "entry requires 'surface'"})
            continue
        if not isinstance(surface, str):
            issues.append({"index": index, "code": CODE_SURFACE_NOT_STRING,
                           "message": "surface must be a string"})
            continue
        if surface == "":
            issues.append({"index": index, "code": CODE_SURFACE_EMPTY,
                           "message": "surface must not be empty"})
            continue

        key = normalize_surface(surface)
        if key == "":
            issues.append({"index": index, "code": CODE_NORMALIZED_EMPTY,
                           "message": f"surface normalizes to empty: {surface!r} masked"})
            continue

        explicit_cost = "cost" in raw and raw.get("cost") is not None
        cost: Optional[float] = None
        frequency = 1

        if explicit_cost:
            cost = _coerce_float(raw.get("cost"))
            if cost is None or cost <= 0:
                issues.append({"index": index, "code": CODE_COST_INVALID,
                               "message": "cost must be a positive number"})
                continue
            # Frequency optional for explicit-cost words; default 1, used only
            # for metadata/inspection, never for cost derivation.
            if "frequency" in raw and raw.get("frequency") is not None:
                frequency = _coerce_int(raw.get("frequency"))
                if frequency is None or frequency < 0:
                    issues.append({"index": index, "code": CODE_FREQUENCY_INVALID,
                                   "message": "frequency must be a non-negative integer"})
                    continue
        else:
            if "frequency" not in raw:
                issues.append({"index": index, "code": CODE_FREQUENCY_INVALID,
                               "message": "frequency is required when cost is not given"})
                continue
            frequency = _coerce_int(raw.get("frequency"))
            if frequency is None or frequency <= 0:
                issues.append({"index": index, "code": CODE_FREQUENCY_INVALID,
                               "message": "frequency must be a positive integer"})
                continue

        prepared.append(PreparedEntry(surface=surface, key=key, frequency=frequency, cost=cost))
        prepared_index.append(index)

    # Duplicate normalized keys are ambiguous (two surfaces normalize alike).
    seen: dict[str, int] = {}
    for pos, entry in enumerate(prepared):
        original_index = prepared_index[pos]
        if entry.key in seen:
            issues.append({
                "index": original_index,
                "code": CODE_DUPLICATE_SURFACE,
                "message": (f"normalized key shared with entry at index {seen[entry.key]} "
                            f"(key length={len(entry.key)})"),
            })
        else:
            seen[entry.key] = original_index

    if issues:
        raise PublishValidationError(issues)
    return prepared
