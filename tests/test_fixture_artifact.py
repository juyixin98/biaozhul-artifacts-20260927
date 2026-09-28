"""Tests for the checked-in synthetic sample data on disk (not regenerated)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from arrowzero.adapters import import_raw_buffers
from arrowzero.kernel.checks import ValidationError, validate_buffers

import pyarrow as pa

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def fixture_doc() -> dict:
    return json.loads((ROOT / "samples" / "fixtures" / "fixtures.json").read_text())


def test_fixture_file_exists_and_has_expected_groups(fixture_doc):
    assert set(fixture_doc) == {"primitive", "strings", "slice_cases", "malformed"}
    assert len(fixture_doc["malformed"]) == 6


def test_malformed_fixtures_each_produce_their_codes(fixture_doc):
    for name, descriptor in fixture_doc["malformed"].items():
        t_name = descriptor["type"]
        t = pa.utf8() if t_name in ("utf8", "string") else getattr(pa, t_name)()
        raw = [
            None if b is None else base64.b64decode(b)
            for b in descriptor["buffers"]
        ]
        violations = validate_buffers(
            t, descriptor["length"], raw, logical_offset=descriptor.get("offset", 0)
        )
        if "expect_violations" in descriptor:
            codes = [v.code.value for v in violations]
            for expected in descriptor["expect_violations"]:
                assert expected in codes, (name, codes)
        else:
            # the valid nonzero-offset descriptor must be importable
            view, _ = import_raw_buffers(descriptor)
            assert view.to_pylist() == descriptor["expect_values"]
