"""Rule/evidence parsing layer.

This is the boundary where externally-shaped evidence (a JSON document with
typed field declarations and record rows) is turned into the strict objects
the cryptographic kernel consumes. Parsing is deliberately separate from
hashing so that rule changes (accepting a new wire shape) never silently alter
commitment semantics.
"""
from __future__ import annotations

from typing import Any

from app.core.batch import FieldSpec
from app.core.encoding import FIELD_TYPES, STATE_MISSING, STATE_NULL, STATE_PRESENT
from app.core.errors import IdentityMismatch, ProofMalformed, TypeEncodingError

_ALLOWED_STATES = frozenset({STATE_PRESENT, STATE_NULL, STATE_MISSING})


def parse_field_specs(raw_fields: Any) -> list[FieldSpec]:
    """Parse the declared schema.

    Accepted shape per field::

        {"path": "holder.age", "type": "int", "value_space": 130}
    """
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ProofMalformed("fields must be a non-empty list")
    specs: list[FieldSpec] = []
    seen: set[str] = set()
    for item in raw_fields:
        if not isinstance(item, dict):
            raise ProofMalformed("each field declaration must be an object")
        path = item.get("path")
        ftype = item.get("type")
        if not isinstance(path, str) or not path:
            raise IdentityMismatch("field.path must be a non-empty string")
        if path in seen:
            raise IdentityMismatch(f"duplicate field path {path!r}")
        seen.add(path)
        if "." in path and any(seg == "" for seg in path.split(".")):
            raise IdentityMismatch(f"field path {path!r} contains an empty segment")
        if ftype not in FIELD_TYPES:
            raise TypeEncodingError(
                f"field {path!r}: type must be one of {sorted(FIELD_TYPES)}, "
                f"got {ftype!r}"
            )
        value_space = item.get("value_space")
        if value_space is not None:
            if not isinstance(value_space, int) or isinstance(value_space, bool):
                raise TypeEncodingError(
                    f"field {path!r}: value_space must be an integer cardinality"
                )
            if value_space < 1:
                raise TypeEncodingError(
                    f"field {path!r}: value_space must be >= 1"
                )
        specs.append(FieldSpec(path=path, field_type=ftype, value_space=value_space))
    return specs


def parse_records(raw_records: Any, specs: list[FieldSpec]) -> list[dict[str, Any]]:
    """Parse record rows against the declared schema.

    Rows are objects keyed by field path. Accepted cell shapes::

        "2026-01-02"                       -> present scalar
        {"value": 42}                      -> present scalar
        {"state": "null"}                  -> explicit null
        {"state": "missing"}               -> explicit missing
        (key absent)                       -> missing
    """
    if not isinstance(raw_records, list) or not raw_records:
        raise ProofMalformed("records must be a non-empty list")
    known = {s.path: s.field_type for s in specs}
    parsed: list[dict[str, Any]] = []
    for rec_idx, row in enumerate(raw_records):
        if not isinstance(row, dict):
            raise ProofMalformed(f"record {rec_idx} must be an object")
        unknown = set(row) - set(known)
        if unknown:
            raise IdentityMismatch(
                f"record {rec_idx} contains undeclared fields: "
                f"{sorted(unknown)}"
            )
        out: dict[str, Any] = {}
        for path, ftype in known.items():
            cell = row.get(path, {"state": STATE_MISSING})
            out[path] = _parse_cell(cell, rec_idx, path)
        parsed.append(out)
    return parsed


def _parse_cell(cell: Any, rec_idx: int, path: str) -> dict[str, Any]:
    if isinstance(cell, dict) and "state" in cell:
        state = cell["state"]
        if state not in _ALLOWED_STATES:
            raise ProofMalformed(
                f"record {rec_idx} field {path!r}: state {state!r} invalid"
            )
        if state == STATE_PRESENT and "value" not in cell:
            raise ProofMalformed(
                f"record {rec_idx} field {path!r}: present state needs value"
            )
        return {"state": state, "value": cell.get("value")}
    if isinstance(cell, dict) and "value" in cell:
        return {"state": STATE_PRESENT, "value": cell["value"]}
    if isinstance(cell, dict):
        raise ProofMalformed(
            f"record {rec_idx} field {path!r}: object cell needs state or value"
        )
    if cell is None:
        return {"state": STATE_NULL, "value": None}
    # Bare scalars mean "present".
    return {"state": STATE_PRESENT, "value": cell}
