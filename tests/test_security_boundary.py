"""Tests for config validation, salt policy and the public/private boundary."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.core.errors import PolicyViolation
from app.security.redaction import assert_no_secret_markers
from app.security.saltpolicy import MIN_SALT_BYTES, SaltPolicy, is_declared_low_entropy


def test_settings_reject_bad_log_level_and_short_salt():
    with pytest.raises(Exception):
        Settings(log_level="LOUD")  # type: ignore[call-arg]
    with pytest.raises(Exception):
        Settings(default_salt_bytes=8)  # type: ignore[call-arg]


def test_settings_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("AUDIT_ALLOW_UNSALTED", "true")
    s = Settings()  # type: ignore[call-arg]
    assert s.allow_unsalted is True
    assert str(s.db_path).endswith("x.db")


def test_salt_policy_minimum():
    SaltPolicy(salt_bytes=MIN_SALT_BYTES)
    with pytest.raises(PolicyViolation):
        SaltPolicy(salt_bytes=12)


def test_low_entropy_detection():
    assert is_declared_low_entropy("bool", None)
    assert is_declared_low_entropy("text", 2)
    assert not is_declared_low_entropy("text", None)
    assert not is_declared_low_entropy("text", 2**24)


def test_public_batch_payload_has_no_salts_or_values():
    public = {
        "batch_id": "b",
        "records": [
            {
                "record_index": 0,
                "fields": [
                    {"path": "a", "commitment_hex": "ff", "state": "present"}
                ],
            }
        ],
    }
    assert assert_no_secret_markers(public) == []


def test_secret_scanner_detects_leaking_keys():
    leaky = {"records": [{"fields": [{"salt_hex": "01", "raw_value": "x"}]}]}
    found = assert_no_secret_markers(leaky)
    assert any("salt_hex" in p for p in found)
    assert any("raw_value" in p for p in found)
