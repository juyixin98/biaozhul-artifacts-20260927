"""Configuration loading (TOML).

A config file declares tables, their columns, the partition transform and the
on-disk layout. Versions are never inferred silently: the running code versions
are compared against the cataloged versions at plan time.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from .transforms import MonthTransform

VALID_TYPES = {"int", "float", "str", "bool", "datetime", "date"}


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str


@dataclass(frozen=True)
class TableSpec:
    name: str
    root: Path
    columns: Dict[str, ColumnSpec]
    transform: MonthTransform | None
    null_label: str


@dataclass(frozen=True)
class Config:
    catalog_db: Path
    base_dir: Path
    tables: Dict[str, TableSpec]


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    base_dir = (raw.get("base_dir") or ".")
    base = (path.parent / base_dir).resolve()
    catalog_db = (path.parent / raw.get("catalog_db", "catalog.db")).resolve()

    tables: Dict[str, TableSpec] = {}
    for t in raw.get("tables", []):
        name = t.get("name")
        if not name:
            raise ConfigError("table missing 'name'")
        cols: Dict[str, ColumnSpec] = {}
        for c in t.get("columns", []):
            cname, ctype = c.get("name"), c.get("type")
            if not cname or ctype not in VALID_TYPES:
                raise ConfigError(f"table {name}: column {cname!r} has bad type {ctype!r}")
            cols[cname] = ColumnSpec(cname, ctype)
        if not cols:
            raise ConfigError(f"table {name}: at least one column required")

        transform = None
        null_label = "__null__"
        ts = t.get("partition")
        if ts:
            kind = ts.get("kind", "month_tz")
            if kind != "month_tz":
                raise ConfigError(f"table {name}: unsupported partition kind {kind!r}")
            src = ts.get("source_column")
            tzname = ts.get("tz", "UTC")
            if src not in cols:
                raise ConfigError(f"table {name}: partition source {src!r} not in columns")
            if cols[src].type != "datetime":
                raise ConfigError(f"table {name}: month partition requires datetime column")
            null_label = ts.get("null_label", null_label)
            transform = MonthTransform(source_column=src, tz_name=tzname,
                                       null_label=null_label)

        root = (base / t.get("path", name)).resolve()
        tables[name] = TableSpec(name=name, root=root, columns=cols,
                                 transform=transform, null_label=null_label)

    if not tables:
        raise ConfigError("no tables declared")
    return Config(catalog_db=catalog_db, base_dir=base, tables=tables)
