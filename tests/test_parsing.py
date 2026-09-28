"""Tests for the rule/evidence parsing boundary."""
from __future__ import annotations

import pytest

from app.core.errors import IdentityMismatch, ProofMalformed, TypeEncodingError
from app.parsing import parse_field_specs, parse_records


def _specs(paths=("a", "b")):
    return parse_field_specs([{"path": p, "type": "text"} for p in paths])


def test_field_specs_require_known_types():
    with pytest.raises(TypeEncodingError):
        parse_field_specs([{"path": "x", "type": "frobnicate"}])


def test_field_specs_reject_duplicate_and_empty_paths():
    with pytest.raises(IdentityMismatch):
        parse_field_specs([{"path": "x", "type": "text"},
                           {"path": "x", "type": "int"}])
    with pytest.raises(ProofMalformed):
        parse_field_specs([])
    with pytest.raises(IdentityMismatch):
        parse_field_specs([{"path": "a..b", "type": "text"}])

def test_records_absent_key_is_missing_explicit_null_is_null():
    specs = _specs()
    rows = parse_records([{"a": "v"}], specs)
    assert rows[0]["a"] == {"state": "present", "value": "v"}
    assert rows[0]["b"] == {"state": "missing", "value": None}

    rows = parse_records([{"a": None, "b": {"state": "null"}}], specs)
    assert rows[0]["a"]["state"] == "null"
    assert rows[0]["b"]["state"] == "null"


def test_records_reject_undeclared_fields():
    specs = _specs()
    with pytest.raises(IdentityMismatch):
        parse_records([{"a": "v", "b": "v", "c": "rogue"}], specs)


def test_records_reject_present_without_value_and_bad_state():
    specs = _specs()
    with pytest.raises(ProofMalformed):
        parse_records([{"a": {"state": "present"}}], specs)
    with pytest.raises(ProofMalformed):
        parse_records([{"a": {"state": "whatever"}}], specs)


def test_bare_scalar_cell_means_present():
    specs = parse_field_specs([{"path": "n", "type": "int"}])
    rows = parse_records([{"n": 7}], specs)
    assert rows[0]["n"] == {"state": "present", "value": 7}
