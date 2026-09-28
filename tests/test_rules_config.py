"""Tests for versioned rule loading and validation failures."""

from __future__ import annotations

import pytest

from secretscan.errors import ConfigError
from secretscan.rules import parse_rules, load_rules

CONFIG = "config/rules.yaml"


def test_shipped_config_loads_with_expected_versions():
    rs = load_rules(CONFIG)
    assert rs.rules_version == "2026.09.01"
    assert rs.classification_version == "1.0.0"
    assert rs.scope.max_file_bytes == 1_048_576
    assert {r.id for r in rs.rules} >= {
        "aws_access_key_id",
        "github_personal_token",
        "pgp_private_key",
        "generic_assignment",
        "bearer_credential",
    }
    # digest must be stable for identical content and change with it
    again = load_rules(CONFIG)
    assert again.raw_digest == rs.raw_digest


def test_missing_secret_named_group_is_rejected():
    bad = """
schema_version: "1.0"
rules_version: "2026.09.02"
classification_version: "1.0.0"
scope: {max_file_bytes: 10, follow_symlinks: false}
ignore: {patterns: []}
entropy: {shannon_threshold: 4.5, min_token_length: 24}
rules:
  - id: broken_rule
    confidence: high
    pattern: 'AKIA[0-9A-Z]{16}'
"""
    with pytest.raises(ConfigError) as exc:
        parse_rules(bad)
    assert exc.value.code == "config_invalid"
    assert "named group" in str(exc.value)


def test_invalid_regex_is_rejected():
    bad = """
schema_version: "1.0"
rules_version: "2026.09.02"
classification_version: "1.0.0"
scope: {max_file_bytes: 10, follow_symlinks: false}
ignore: {patterns: []}
entropy: {shannon_threshold: 4.5, min_token_length: 24}
rules:
  - id: broken_rule
    pattern: '(?P<secret>[0-9+'
"""
    with pytest.raises(ConfigError) as exc:
        parse_rules(bad)
    assert "invalid regex" in str(exc.value)


def test_unsupported_schema_and_bad_version_string():
    base = """
schema_version: "9.9"
rules_version: "2026.09.02"
classification_version: "1.0.0"
scope: {max_file_bytes: 10, follow_symlinks: false}
ignore: {patterns: []}
entropy: {shannon_threshold: 4.5, min_token_length: 24}
rules:
  - id: ok_rule
    pattern: '(?P<secret>x+)'
"""
    with pytest.raises(ConfigError, match="unsupported schema"):
        parse_rules(base)

    badver = base.replace('"9.9"', '"1.0"').replace('"2026.09.02"', '"v2"')
    with pytest.raises(ConfigError, match="date-based version"):
        parse_rules(badver)


def test_per_rule_entropy_above_global_floor_rejected():
    bad = """
schema_version: "1.0"
rules_version: "2026.09.02"
classification_version: "1.0.0"
scope: {max_file_bytes: 10, follow_symlinks: false}
ignore: {patterns: []}
entropy: {shannon_threshold: 4.5, min_token_length: 24}
rules:
  - id: greedy_rule
    min_entropy: 7.0
    pattern: '(?P<secret>x+)'
"""
    with pytest.raises(ConfigError, match="above the global"):
        parse_rules(bad)
