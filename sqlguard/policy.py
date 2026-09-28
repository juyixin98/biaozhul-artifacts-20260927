"""Policy (declared whitelist) loading and lookup."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TablePolicy:
    columns: frozenset[str]
    allow: frozenset[str]


@dataclass(frozen=True)
class SlotPolicy:
    # roles this slot may play
    roles: frozenset[str]
    # table-role: global allowed list
    allowed_tables: frozenset[str] = frozenset()
    # column-role: per-table allowed columns
    allowed_columns: dict[str, frozenset[str]] = field(default_factory=dict)
    # column-role: may bind to any whitelisted column without explicit list
    allow_unqualified_columns: bool = False
    # keyword-role: allowed keywords (normalized upper-case)
    allowed_keywords: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Policy:
    policy_id: str
    description: str
    dialect: str
    allowed_statements: frozenset[str]
    max_array_length: int
    allow_empty_array: bool
    tables: dict[str, TablePolicy]
    slots: dict[str, SlotPolicy]

    def table(self, name: str) -> TablePolicy | None:
        return self.tables.get(name.lower())

    def slot(self, name: str) -> SlotPolicy | None:
        return self.slots.get(name)

    def known_column(self, table_name: str, column_name: str) -> bool:
        tp = self.table(table_name)
        if tp is None:
            return False
        return column_name.lower() in tp.columns


def load_policy(path: str | Path) -> Policy:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tables = {
        name.lower(): TablePolicy(
            frozenset(c.lower() for c in spec["columns"]),
            frozenset(a.lower() for a in spec.get("allow", [])),
        )
        for name, spec in data.get("tables", {}).items()
    }
    slots: dict[str, SlotPolicy] = {}
    for name, spec in data.get("identifier_slots", {}).items():
        roles = frozenset(spec["roles"])
        allowed = spec.get("allowed", None)
        allowed_tables: frozenset[str] = frozenset()
        allowed_columns: dict[str, frozenset[str]] = {}
        allowed_keywords: frozenset[str] = frozenset()
        if isinstance(allowed, list):
            if "table" in roles:
                allowed_tables = frozenset(v.lower() for v in allowed)
            elif "keyword" in roles:
                allowed_keywords = frozenset(v.upper() for v in allowed)
            elif "column" in roles:
                # global list applies to every whitelisted table
                vals = frozenset(v.lower() for v in allowed)
                allowed_columns = {t: vals for t in tables}
        elif isinstance(allowed, dict):
            allowed_columns = {
                t.lower(): frozenset(c.lower() for c in cols)
                for t, cols in allowed.items()
            }
        slots[name] = SlotPolicy(
            roles=roles,
            allowed_tables=allowed_tables,
            allowed_columns=allowed_columns,
            allow_unqualified_columns=bool(
                spec.get("allow_unqualified_columns", False)
            ),
            allowed_keywords=allowed_keywords,
        )
    return Policy(
        policy_id=data["policy_id"],
        description=data.get("description", ""),
        dialect=data.get("dialect", "sqlite"),
        allowed_statements=frozenset(
            s.lower() for s in data.get("allowed_statements", ["select"])
        ),
        max_array_length=int(data.get("max_array_length", 1000)),
        allow_empty_array=bool(data.get("allow_empty_array", False)),
        tables=tables,
        slots=slots,
    )
