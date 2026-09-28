"""Batch schema and record parsing (rule layer).

A batch is defined by an *ordered* field schema. Order is part of the
commitment identity: the schema position is bound into every field
commitment, so two fields with identical names-and-values at different
positions cannot be interchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

from .types import CanonicalEncodeError, FieldType, canonical_validate_field_name


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: FieldType
    salted: bool = True  # unsalted commitments are allowed but flagged as enumerable

    def __post_init__(self) -> None:
        canonical_validate_field_name(self.name)
        if not isinstance(self.type, FieldType):
            object.__setattr__(self, "type", FieldType(self.type))
        if not isinstance(self.salted, bool):
            raise CanonicalEncodeError("salted must be a boolean")


@dataclass(frozen=True)
class BatchSchema:
    fields: tuple[FieldSpec, ...]

    @classmethod
    def from_dicts(cls, raw: list[dict]) -> "BatchSchema":
        if not isinstance(raw, list) or not raw:
            raise CanonicalEncodeError("schema must be a non-empty list of field specs")
        specs: list[FieldSpec] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise CanonicalEncodeError(f"field spec must be an object, got {type(item).__name__}")
            name = item.get("name")
            type_raw = item.get("type")
            salted = item.get("salted", True)
            spec = FieldSpec(name=name, type=FieldType(type_raw), salted=bool(salted))
            if spec.name in seen:
                raise CanonicalEncodeError(f"duplicate field name in schema: {spec.name!r}")
            seen.add(spec.name)
            specs.append(spec)
        return cls(tuple(specs))

    def index_of(self, name: str) -> int:
        for i, spec in enumerate(self.fields):
            if spec.name == name:
                return i
        raise KeyError(name)

    def to_dicts(self) -> list[dict]:
        return [{"name": s.name, "type": s.type.value, "salted": s.salted} for s in self.fields]


def parse_records(raw: object) -> list[dict]:
    """Validate the raw records payload shape; values stay untyped here."""
    if not isinstance(raw, list) or not raw:
        raise CanonicalEncodeError("records must be a non-empty list")
    records: list[dict] = []
    for pos, rec in enumerate(raw):
        if not isinstance(rec, dict):
            raise CanonicalEncodeError(f"record {pos} must be an object")
        for key in rec:
            canonical_validate_field_name(str(key))
        records.append(rec)
    return records
