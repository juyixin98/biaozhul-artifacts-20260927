"""Data contract shared by adapter, planner, executor, service and API.

A merge request is normalized once (via :meth:`MergeSpec.from_payload`) into
frozen dataclasses; every downstream module consumes those, never raw dicts.
That single normalization point is what lets "input error" be one category no
matter which layer discovers the bad field.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import SPEC_INVALID_CODE, InputError


class NullPolicy(str, Enum):
    """NULL equality strategy for *key matching* only.

    SQL_NOT_DISTINCT
        ``NULL IS NOT DISTINCT FROM NULL`` is true: two NULL key components are
        equal (this is the SQL ``MATCH``/``IS`` semantics and the default).
    SQL_DISTINCT
        ``NULL = NULL`` is unknown: a NULL key component can never match
        anything, so such source rows are always NOT MATCHED and NULL-bearing
        keys never collide with each other.

    Note: WHEN-condition predicates are unaffected - there NULL comparisons
    always follow SQL three-valued logic (NULL = NULL -> unknown).
    """

    SQL_NOT_DISTINCT = "NULLS_NOT_DISTINCT"
    SQL_DISTINCT = "NULLS_DISTINCT"


class ClauseType(str, Enum):
    MATCHED_UPDATE = "WHEN_MATCHED_THEN_UPDATE"
    MATCHED_DELETE = "WHEN_MATCHED_THEN_DELETE"
    NOT_MATCHED_INSERT = "WHEN_NOT_MATCHED_THEN_INSERT"


_ALLOWED_ACTIONS = {
    "update": ClauseType.MATCHED_UPDATE,
    "delete": ClauseType.MATCHED_DELETE,
    "insert": ClauseType.NOT_MATCHED_INSERT,
}

# Clause kinds allowed per match side; checked during normalization so the
# planner never has to defend against an impossible combination.
_ALLOWED_FOR_MATCHED = {ClauseType.MATCHED_UPDATE, ClauseType.MATCHED_DELETE}
_ALLOWED_FOR_NOT_MATCHED = {ClauseType.NOT_MATCHED_INSERT}


@dataclass(frozen=True)
class WhenClause:
    """One WHEN rule. Rules fire in ``order`` (list order), first match wins."""

    order: int
    matched: bool
    type: ClauseType
    condition: str | None
    # UPDATE: target-column -> expression text evaluated over S.* / T.*
    # INSERT: target-column -> expression text evaluated over S.* only
    assignments: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MergeSpec:
    """Fully normalized merge specification."""

    target_table: str
    key_columns: tuple[str, ...]
    update_columns: tuple[str, ...]
    insert_columns: tuple[str, ...]
    null_policy: NullPolicy
    clauses: tuple[WhenClause, ...]
    max_source_rows: int = 1_000_000

    # ------------------------------------------------------------------ parse
    @classmethod
    def from_payload(cls, payload: Any) -> "MergeSpec":
        """Validate shape and normalize a JSON-able payload into a MergeSpec.

        Only shape/contract checks happen here (identifiers, enums, clause
        consistency). Column-existence checks against target/source schemas
        live in :mod:`merge_engine.planner`, where both schemas are known.
        """
        if not isinstance(payload, dict):
            raise _spec("request body must be a JSON object", {"got": type(payload).__name__})

        table = payload.get("target_table")
        if not isinstance(table, str) or not table:
            raise _spec("'target_table' must be a non-empty string")
        _check_identifier(table, "target_table")

        key_columns = _check_name_list(payload.get("key_columns"), "key_columns", min_len=1)
        if len(set(key_columns)) != len(key_columns):
            raise _spec("'key_columns' must not contain duplicates", {"key_columns": key_columns})

        update_columns = _check_name_list(
            payload.get("update_columns", []), "update_columns"
        )
        insert_columns = _check_name_list(
            payload.get("insert_columns", []), "insert_columns"
        )
        _check_identifier_overlap(update_columns, "update_columns", key_columns)
        _check_identifier_overlap(insert_columns, "insert_columns", key_columns)

        policy = NullPolicy.SQL_NOT_DISTINCT
        if "null_policy" in payload and payload["null_policy"] is not None:
            try:
                policy = NullPolicy(payload["null_policy"])
            except ValueError:
                raise _spec(
                    "'null_policy' must be one of "
                    f"{[p.value for p in NullPolicy]}",
                    {"got": payload["null_policy"]},
                )

        raw_clauses = payload.get("when_clauses")
        if not isinstance(raw_clauses, list) or not raw_clauses:
            raise _spec("'when_clauses' must be a non-empty list")

        clauses: list[WhenClause] = []
        for i, raw in enumerate(raw_clauses):
            clauses.append(_parse_clause(raw, i))

        max_rows = int(payload.get("max_source_rows", 1_000_000))
        if max_rows <= 0:
            raise _spec("'max_source_rows' must be a positive integer", {"got": max_rows})

        return cls(
            target_table=table,
            key_columns=tuple(key_columns),
            update_columns=tuple(update_columns),
            insert_columns=tuple(insert_columns),
            null_policy=policy,
            clauses=tuple(clauses),
            max_source_rows=max_rows,
        )


def _parse_clause(raw: Any, order: int) -> WhenClause:
    where = f"when_clauses[{order}]"
    if not isinstance(raw, dict):
        raise _spec(f"{where} must be an object")

    matched_raw = raw.get("matched")
    if not isinstance(matched_raw, bool):
        raise _spec(f"{where}.matched must be true or false")

    action = raw.get("action")
    if not isinstance(action, str) or action.lower() not in _ALLOWED_ACTIONS:
        raise _spec(
            f"{where}.action must be one of {sorted(_ALLOWED_ACTIONS)}",
            {"got": action},
        )
    ctype = _ALLOWED_ACTIONS[action.lower()]
    if matched_raw and ctype not in _ALLOWED_FOR_MATCHED:
        raise _spec(
            f"{where}: action {action!r} is only valid for NOT MATCHED rows",
        )
    if not matched_raw and ctype not in _ALLOWED_FOR_NOT_MATCHED:
        raise _spec(
            f"{where}: action {action!r} is only valid for MATCHED rows",
        )

    condition = raw.get("condition")
    if condition is not None and (not isinstance(condition, str) or not condition.strip()):
        raise _spec(f"{where}.condition must be a non-empty string or null")

    assignments = _parse_assignments(raw.get("assignments"), where, ctype)
    return WhenClause(
        order=order,
        matched=matched_raw,
        type=ctype,
        condition=condition.strip() if condition else None,
        assignments=assignments,
    )


def _parse_assignments(
    raw: Any, where: str, ctype: ClauseType
) -> dict[str, str]:
    if ctype == ClauseType.MATCHED_DELETE:
        if raw:
            raise _spec(f"{where}: DELETE clause must not carry 'assignments'")
        return {}

    if not isinstance(raw, dict) or not raw:
        label = "UPDATE" if ctype == ClauseType.MATCHED_UPDATE else "INSERT"
        raise _spec(f"{where}: {label} clause requires a non-empty 'assignments' object")

    out: dict[str, str] = {}
    for col, expr in raw.items():
        if not isinstance(col, str) or not col:
            raise _spec(f"{where}.assignments keys must be non-empty column names")
        _check_identifier(col, f"{where}.assignments")
        if not isinstance(expr, str) or not expr.strip():
            raise _spec(
                f"{where}.assignments[{col!r}] must be a non-empty expression string"
            )
        out[col] = expr.strip()
    return out


def _check_name_list(raw: Any, field_name: str, *, min_len: int = 0) -> list[str]:
    if not isinstance(raw, list):
        raise _spec(f"{field_name!r} must be a list of column names")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise _spec(f"{field_name!r} entries must be non-empty strings", {"got": item})
        _check_identifier(item, field_name)
        out.append(item)
    if len(out) < min_len:
        raise _spec(f"{field_name!r} must contain at least {min_len} column(s)")
    if len(set(out)) != len(out):
        raise _spec(f"{field_name!r} must not contain duplicates", {"values": out})
    return out


def _check_identifier(name: str, where: str) -> None:
    # Identifiers are double-quote escaped everywhere; still restrict the
    # alphabet so metadata/log keys stay readable and accidental SQL is obvious.
    if not isinstance(name, str) or not name:
        raise _spec(f"{where!r} must be a non-empty identifier")
    if not (name[0].isalpha() or name[0] == "_") or not all(
        c.isalnum() or c == "_" for c in name
    ):
        raise _spec(
            f"{where!r} must start with a letter/underscore and contain only "
            "letters, digits and underscores",
            {"got": name},
        )


def _check_identifier_overlap(cols: list[str], label: str, keys: list[str]) -> None:
    overlap = sorted(set(cols) & set(keys))
    if overlap:
        raise _spec(
            f"{label!r} must not contain key columns (keys are preserved on "
            "UPDATE and supplied on INSERT through key_columns)",
            {"overlap": overlap},
        )


def _spec(message: str, details: dict[str, Any] | None = None) -> InputError:
    return InputError(SPEC_INVALID_CODE, message, details=details)


# --------------------------------------------------------------------- rows
@dataclass(frozen=True)
class SourceRow:
    """One canonical source record. ``index`` is the original 0-based position
    in the source batch (used for replay); matching itself never uses it."""

    index: int
    values: dict[str, Any]


@dataclass(frozen=True)
class TargetRow:
    """One pre-operation target record plus its SQLite rowid."""

    rowid: int
    values: dict[str, Any]


# ------------------------------------------------------------------- outcome
class Outcome(str, Enum):
    UPDATE = "UPDATE"
    INSERT = "INSERT"
    DELETE = "DELETE"
    UNPROCESSED = "UNPROCESSED"  # no WHEN rule fired


@dataclass(frozen=True)
class PlannedAction:
    """One concrete, already-evaluated decision.

    ``new_values`` holds the fully evaluated row to INSERT, or the
    column->value subset to UPDATE. ``source_index`` / ``target_rowid`` anchor
    the action back to the input for logs and replay.
    """

    seq: int
    outcome: Outcome
    source_index: int
    target_rowid: int | None
    key: tuple[Any, ...]
    new_values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """Human/audit view of why a source row got its action."""

    source_index: int
    key: tuple[Any, ...]
    matched: bool
    target_rowid: int | None
    outcome: Outcome
    fired_clause: int | None
    reason: str
    clause_results: tuple[dict[str, Any], ...] = ()


@dataclass
class MergePlan:
    """Result of the pure decision phase - no SQL has run yet.

    The plan is the validation/execution contract: validate returns it;
    executor only consumes it. ``actions`` is deterministic: source rows are
    processed in a key order independent of input ordering.
    """

    spec: MergeSpec
    actions: list[PlannedAction]
    decisions: list[Decision]
    source_count: int
    target_count: int
    source_fingerprint: str
    target_fingerprint: str
    # snapshot data kept for atomicity re-checks and metadata replay
    target_rows: list[TargetRow] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = {"update": 0, "insert": 0, "delete": 0, "unprocessed": 0}
        for a in self.actions:
            c[a.outcome.value.lower()] += 1
        return c

    def summary(self) -> dict[str, Any]:
        c = self.counts()
        return {
            "source_rows": self.source_count,
            "target_rows": self.target_count,
            "actions": {k: v for k, v in c.items() if v or k != "unprocessed"},
            "unprocessed": c["unprocessed"],
            "source_fingerprint": self.source_fingerprint,
            "target_fingerprint": self.target_fingerprint,
        }


def as_public_dict(obj: Any) -> Any:
    """Dataclass -> JSON-able dict (tuples -> lists, enums -> values)."""
    if dataclasses.is_dataclass(obj):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            out[f.name] = as_public_dict(getattr(obj, f.name))
        return out
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [as_public_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: as_public_dict(v) for k, v in obj.items()}
    return obj
