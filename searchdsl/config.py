"""Standalone configuration.

Configuration is loaded from JSON (``config.json`` by convention) and is
fully independent of the parser/index modules — nothing else reads
environment variables or hard-coded paths. Defaults live in
:data:`DEFAULT_CONFIG` and can also be used directly in tests.

The ``limits`` section is the *complexity budget* enforced before
execution by :mod:`searchdsl.validate`:

  max_query_bytes   raw UTF-8 size of the query string
  max_nesting_depth maximum nesting depth of the parsed tree
                    (a bare leaf has depth 1)
  max_clauses       maximum number of leaf clauses (term/phrase/range)
  max_query_terms   maximum number of analyzed tokens across all clauses
  max_phrase_terms  maximum number of tokens inside a single phrase
  max_result_window maximum ``offset + limit`` a client may request
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG: dict = {
    "limits": {
        "max_query_bytes": 4096,
        "max_nesting_depth": 12,
        "max_clauses": 64,
        "max_query_terms": 256,
        "max_phrase_terms": 16,
        "max_result_window": 1000,
    },
    "search": {"default_limit": 10, "max_limit": 100},
    "paths": {
        "schema": "fixtures/schema.json",
        "corpus": "fixtures/corpus.jsonl",
        "database": "data/searchdsl.db",
    },
    "server": {"host": "127.0.0.1", "port": 8000},
}


@dataclass(frozen=True)
class Limits:
    max_query_bytes: int = 4096
    max_nesting_depth: int = 12
    max_clauses: int = 64
    max_query_terms: int = 256
    max_phrase_terms: int = 16
    max_result_window: int = 1000


@dataclass(frozen=True)
class SearchSettings:
    default_limit: int = 10
    max_limit: int = 100


@dataclass(frozen=True)
class Paths:
    schema: str = "fixtures/schema.json"
    corpus: str = "fixtures/corpus.jsonl"
    database: str = "data/searchdsl.db"


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True)
class Config:
    limits: Limits = field(default_factory=Limits)
    search: SearchSettings = field(default_factory=SearchSettings)
    paths: Paths = field(default_factory=Paths)
    server: ServerSettings = field(default_factory=ServerSettings)

    def with_overrides(self, **sections) -> "Config":
        parts = {
            "limits": self.limits,
            "search": self.search,
            "paths": self.paths,
            "server": self.server,
        }
        for key, values in sections.items():
            if key not in parts:
                raise KeyError(f"unknown config section: {key!r}")
            parts[key] = replace(parts[key], **values)
        return Config(**parts)


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def config_from_dict(data: Optional[dict]) -> Config:
    merged = _merge(DEFAULT_CONFIG, data or {})
    return Config(
        limits=Limits(**merged["limits"]),
        search=SearchSettings(**merged["search"]),
        paths=Paths(**merged["paths"]),
        server=ServerSettings(**merged["server"]),
    )


def load_config(path: Optional[str | Path] = None) -> Config:
    """Load config JSON; fall back to defaults when *path* is None/omitted."""
    if path is None:
        return config_from_dict(None)
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    return config_from_dict(json.loads(p.read_text(encoding="utf-8")))
