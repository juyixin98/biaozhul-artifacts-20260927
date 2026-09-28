"""Shared pytest fixtures/helpers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.schema import Schema  # noqa: E402
from app.core.kernel import encode_table  # noqa: E402


@pytest.fixture
def fixture_data() -> dict:
    with open(ROOT / "fixtures" / "expected_trees.json") as fh:
        return json.load(fh)


@pytest.fixture
def case(fixture_data):
    def _get(name: str) -> dict:
        for c in fixture_data["cases"]:
            if c["name"] == name:
                return c
        raise KeyError(name)
    return _get


@pytest.fixture
def encoded():
    def _encode(spec: dict, records: list):
        return encode_table(Schema(spec), records)
    return _encode
