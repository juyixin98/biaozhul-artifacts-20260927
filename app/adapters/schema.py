"""Schema adapter.

Responsibility (format adaptation only):
- normalise a PyArrow schema into the canonical, serialisable form stored in
  the metadata DB;
- decide whether a new data file's schema is compatible with the table schema.

It contains no transaction logic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pyarrow as pa

from app.kernel.errors import ErrorCategory, ServiceError


@dataclass(frozen=True)
class CanonicalSchema:
    """Canonical table schema: ordered (name, pyarrow type string) pairs."""

    columns: tuple[tuple[str, str], ...]

    @classmethod
    def from_arrow(cls, schema: pa.Schema) -> "CanonicalSchema":
        return cls(tuple((f.name, str(f.type)) for f in schema))

    def to_json(self) -> str:
        return json.dumps([{"name": n, "type": t} for n, t in self.columns])

    @classmethod
    def from_json(cls, raw: str) -> "CanonicalSchema":
        data = json.loads(raw)
        return cls(tuple((c["name"], c["type"]) for c in data))

    def names(self) -> tuple[str, ...]:
        return tuple(n for n, _ in self.columns)

    def as_dict(self) -> dict[str, str]:
        return dict(self.columns)


def ensure_partition_columns(
    schema: pa.Schema, partition_spec: tuple[str, ...]
) -> None:
    """Reject files whose schema does not carry every partition column."""
    present = set(schema.names)
    missing = [c for c in partition_spec if c not in present]
    if missing:
        raise ServiceError(
            ErrorCategory.VALIDATION,
            f"parquet file is missing partition column(s): {missing}",
            details={"missing": missing, "partition_spec": list(partition_spec)},
        )


def check_compatible(
    file_schema: pa.Schema,
    table_schema: CanonicalSchema | None,
    partition_spec: tuple[str, ...],
) -> CanonicalSchema:
    """Return the canonical schema to use.

    - ``table_schema is None``  -> the file defines the table schema.
    - otherwise every (name, type) pair must match exactly.  A missing column
      or a different type (e.g. int64 vs string) is a VALIDATION rejection.
    """
    ensure_partition_columns(file_schema, partition_spec)
    file_canonical = CanonicalSchema.from_arrow(file_schema)
    if table_schema is None:
        return file_canonical
    if file_canonical != table_schema:
        incoming = file_canonical.as_dict()
        existing = table_schema.as_dict()
        mismatches: list[dict[str, str]] = []
        for name, existing_type in existing.items():
            incoming_type = incoming.get(name)
            if incoming_type != existing_type:
                mismatches.append(
                    {
                        "column": name,
                        "expected": existing_type,
                        "actual": incoming_type or "<missing>",
                    }
                )
        extra = [n for n in incoming if n not in existing]
        raise ServiceError(
            ErrorCategory.VALIDATION,
            "parquet file schema is incompatible with the table schema",
            details={"mismatches": mismatches, "extra_columns": extra},
        )
    return table_schema
