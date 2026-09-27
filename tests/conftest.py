"""Shared pytest fixtures.

The engine used across the test suite is built on an in-memory SQLite
index from the shipped fixtures, so tests never touch a shared on-disk
state and never need network access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from searchdsl.config import config_from_dict  # noqa: E402
from searchdsl.search import SearchEngine  # noqa: E402
from searchdsl.spec import load_schema  # noqa: E402
from searchdsl.store import Store  # noqa: E402

FIXTURES = ROOT / "fixtures"
CORPUS_DOCS = [
    json.loads(line)
    for line in (FIXTURES / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]


@pytest.fixture(scope="session")
def schema():
    return load_schema(FIXTURES / "schema.json")


@pytest.fixture(scope="session")
def corpus_docs():
    return CORPUS_DOCS


@pytest.fixture
def engine():
    cfg = config_from_dict(
        {"paths": {"schema": str(FIXTURES / "schema.json"),
                   "corpus": str(FIXTURES / "corpus.jsonl"),
                   "database": ":memory:"}}
    )
    store = Store(":memory:")
    eng = SearchEngine(cfg, store=store)
    try:
        yield eng
    finally:
        eng.close()


@pytest.fixture
def small_budget_engine():
    cfg = config_from_dict(
        {
            "limits": {"max_nesting_depth": 3, "max_clauses": 4,
                       "max_query_terms": 8, "max_phrase_terms": 3},
            "paths": {"schema": str(FIXTURES / "schema.json"),
                      "corpus": str(FIXTURES / "corpus.jsonl"),
                      "database": ":memory:"},
        }
    )
    store = Store(":memory:")
    eng = SearchEngine(cfg, store=store)
    try:
        yield eng
    finally:
        eng.close()
