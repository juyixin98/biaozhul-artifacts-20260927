"""Declarative review policy: statement allow-list, identifier whitelists,
bind-parameter allow-lists, and structural guards.

A policy is plain data loaded from YAML/JSON; *inline* overrides can be
supplied per request (see :class:`ReviewRequest`) and are merged over the file
policy. The kernel never invents whitelists — an identifier is accepted only
when a matching declaration exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SlotPolicy:
    allowed_identifiers: frozenset[str] = frozenset()
    default: str | None = None
    # reject when the rendered identifier cannot also be found in the fixture
    require_in_schema: bool = True
    # scope the slot: relation / column / sort / any
    scope: str = "any"
    # keyword slots (ASC/DESC) render as bare words, never quoted identifiers
    quote: bool = True
    # for bare-word slots, the chosen value is validated against this strict
    # keyword allow-list shape (letters/underscore only) at render time
    bare_word_pattern: str = r"^[A-Za-z_][A-Za-z0-9_]*$"


@dataclass(frozen=True)
class ParamPolicy:
    # when non-empty, bound values must be members
    allowed_values: frozenset[Any] = frozenset()
    allow_array: bool = False
    scalar_types: frozenset[str] = frozenset({"null", "int", "float", "str", "bool", "bytes"})


@dataclass(frozen=True)
class Policy:
    allowed_statement_types: frozenset[str] = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
    slots: dict[str, SlotPolicy] = field(default_factory=dict)
    params: dict[str, ParamPolicy] = field(default_factory=dict)
    writable_tables: frozenset[str] = frozenset()
    known_tables: frozenset[str] = frozenset()
    require_where_for_update_delete: bool = True
    check_static_tables: bool = True
    check_static_columns: bool = True
    max_array_length: int = 1000

    def slot(self, name: str) -> SlotPolicy | None:
        return self.slots.get(name)

    def param(self, key: str) -> ParamPolicy:
        # named markers use their bare name; positional int keys fall back to
        # the wildcard '*' policy if one is declared
        return self.params.get(key) or self.params.get("*") or ParamPolicy()

    def with_overrides(self, inline: dict[str, Any] | None) -> "Policy":
        if not inline:
            return self
        slots = dict(self.slots)
        for name, spec in (inline.get("slots") or {}).items():
            base = self.slots.get(name, SlotPolicy())
            slots[name] = SlotPolicy(
                allowed_identifiers=frozenset(
                    spec.get("allowed", base.allowed_identifiers)),
                default=spec.get("default", base.default),
                require_in_schema=spec.get("require_in_schema", base.require_in_schema),
                scope=spec.get("scope", base.scope),
                quote=spec.get("quote", base.quote),
            )
        params = dict(self.params)
        for name, spec in (inline.get("params") or {}).items():
            base = self.params.get(name, ParamPolicy())
            params[name] = ParamPolicy(
                allowed_values=frozenset(spec.get("allowed_values", base.allowed_values)),
                allow_array=spec.get("allow_array", base.allow_array),
                scalar_types=base.scalar_types,
            )
        return Policy(
            allowed_statement_types=self.allowed_statement_types,
            slots=slots,
            params=params,
            writable_tables=self.writable_tables,
            known_tables=self.known_tables,
            require_where_for_update_delete=self.require_where_for_update_delete,
            check_static_tables=self.check_static_tables,
            check_static_columns=self.check_static_columns,
            max_array_length=self.max_array_length,
        )


def _freeze(values: Any) -> frozenset[Any]:
    if values is None:
        return frozenset()
    return frozenset(values)


def load_policy(path: str | Path) -> Policy:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if p.suffix in {".yaml", ".yml"} else json.loads(text)
    return policy_from_dict(data or {})


def policy_from_dict(data: dict[str, Any]) -> Policy:
    slots = {
        name: SlotPolicy(
            allowed_identifiers=_freeze(spec.get("allowed")),
            default=spec.get("default"),
            require_in_schema=bool(spec.get("require_in_schema", True)),
            scope=spec.get("scope", "any"),
            quote=bool(spec.get("quote", True)),
        )
        for name, spec in (data.get("slots") or {}).items()
    }
    params = {
        name: ParamPolicy(
            allowed_values=_freeze(spec.get("allowed_values")),
            allow_array=bool(spec.get("allow_array", False)),
        )
        for name, spec in (data.get("params") or {}).items()
    }
    return Policy(
        allowed_statement_types=_freeze(
            (data.get("allowed_statement_types")
             or ["SELECT", "INSERT", "UPDATE", "DELETE"])),
        slots=slots,
        params=params,
        writable_tables=_freeze(data.get("writable_tables")),
        known_tables=_freeze(data.get("known_tables")),
        require_where_for_update_delete=bool(
            data.get("require_where_for_update_delete", True)),
        check_static_tables=bool(data.get("check_static_tables", True)),
        check_static_columns=bool(data.get("check_static_columns", True)),
        max_array_length=int(data.get("max_array_length", 1000)),
    )
