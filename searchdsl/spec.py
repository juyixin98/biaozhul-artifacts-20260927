"""Field whitelist and text specification.

Four field types are supported:

  text     analyzed, tokenized; supports term + phrase; case-insensitive.
           Values are strings. Analyzer also splits on ``_`` and digits
           boundaries (see :mod:`searchdsl.analysis`).
  keyword  exact-match, case-sensitive; supports term only; may be
           multi-valued (a list of strings).
  int      whole numbers; supports term (exact) and range.
  date     ISO-8601 calendar dates (``YYYY-MM-DD``); supports term
           (exact) and range; compared lexicographically on the
           normalized form.

An unfielded query (no ``field:`` prefix) searches the schema's
``default_fields`` (text fields only). Unknown fields are rejected
*before execution* by :mod:`searchdsl.validate`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

VALID_TYPES = frozenset({"text", "keyword", "int", "date"})


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    multi_valued: bool = False

    def __post_init__(self):
        if self.type not in VALID_TYPES:
            raise ValueError(f"invalid field type for {self.name!r}: {self.type!r}")

    def supports_phrase(self) -> bool:
        return self.type == "text"

    def supports_range(self) -> bool:
        return self.type in {"int", "date"}

    def supports_term(self) -> bool:
        return True


@dataclass(frozen=True)
class Schema:
    fields: dict[str, FieldSpec]
    default_fields: tuple[str, ...]
    title_field: str = "doc_id"

    def get(self, name: str) -> FieldSpec:
        return self.fields[name]

    def has(self, name: str) -> bool:
        return name in self.fields

    def as_dict(self) -> dict:
        return {
            "fields": {
                name: {"type": fs.type, "multi_valued": fs.multi_valued}
                for name, fs in sorted(self.fields.items())
            },
            "default_fields": list(self.default_fields),
        }


def load_schema(path: str | Path) -> Schema:
    """Load a schema JSON file of the form::

        {
          "fields": {"title": {"type": "text"},
                     "tags": {"type": "keyword", "multi_valued": true}},
          "default_fields": ["title", "body"]
        }
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return schema_from_dict(data)


def schema_from_dict(data: dict) -> Schema:
    fields: dict[str, FieldSpec] = {}
    for name, spec in data["fields"].items():
        if not isinstance(name, str) or not name:
            raise ValueError("field names must be non-empty strings")
        fields[name] = FieldSpec(
            name=name,
            type=spec["type"],
            multi_valued=bool(spec.get("multi_valued", False)),
        )
    defaults = tuple(data.get("default_fields", ()))
    for d in defaults:
        if d not in fields:
            raise ValueError(f"default field {d!r} is not declared")
        if fields[d].type != "text":
            raise ValueError(f"default field {d!r} must be of type text")
    title_field = data.get("title_field", "doc_id")
    return Schema(fields=fields, default_fields=defaults, title_field=title_field)
