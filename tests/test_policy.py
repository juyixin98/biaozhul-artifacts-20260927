"""Policy loading and per-request override tests."""

from __future__ import annotations

import json
from pathlib import Path

from sqlguard.core.policy import (
    ParamPolicy,
    Policy,
    SlotPolicy,
    load_policy,
    policy_from_dict,
)


def test_policy_from_dict_defaults():
    p = policy_from_dict({})
    assert p.allowed_statement_types == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert p.require_where_for_update_delete is True
    assert p.slots == {}


def test_policy_slot_and_param_declarations():
    p = policy_from_dict({
        "slots": {"sort": {"allowed": ["id", "name"], "default": "id"}},
        "params": {"role": {"allowed_values": ["admin"]}},
        "writable_tables": ["users"],
    })
    assert p.slot("sort").allowed_identifiers == {"id", "name"}
    assert p.slot("sort").default == "id"
    assert p.param("role").allowed_values == {"admin"}
    assert "users" in p.writable_tables


def test_unknown_slot_and_param_return_safe_defaults():
    p = policy_from_dict({})
    assert p.slot("nope") is None
    # unknown params get an empty (permissive-type) policy, not an exception
    assert isinstance(p.param("anything"), ParamPolicy)


def test_wildcard_param_policy_applies_to_unnamed():
    p = policy_from_dict({"params": {"*": {"allow_array": True}}})
    assert p.param("0").allow_array is True


def test_inline_override_extends_without_mutating_base():
    base = policy_from_dict({"slots": {"a": {"allowed": ["x"]}}})
    override = {"slots": {"b": {"allowed": ["y"], "scope": "sort"}}}
    effective = base.with_overrides(override)
    assert base.slot("b") is None          # base untouched
    assert effective.slot("b").allowed_identifiers == {"y"}
    assert effective.slot("a").allowed_identifiers == {"x"}


def test_inline_override_none_returns_same_policy():
    p = policy_from_dict({})
    assert p.with_overrides(None) is p
    assert p.with_overrides({}) is p


def test_load_yaml_policy_from_repo_config():
    p = load_policy("config/policy.yaml")
    assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= p.allowed_statement_types
    assert "sort_col" in p.slots
    assert "sort_dir" in p.slots
    assert p.slot("sort_dir").quote is False
    assert "users" in p.writable_tables


def test_load_json_policy(tmp_path: Path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"slots": {"t": {"allowed": ["a"]}}}))
    p = load_policy(f)
    assert p.slot("t").allowed_identifiers == {"a"}
