"""Unit tests for the configuration layer."""

from __future__ import annotations

import pytest

from arrowzero.config import Settings

pytestmark = pytest.mark.unit


def test_defaults_are_local_and_runnable(monkeypatch):
    for key in [
        "ARROWZERO_DB_PATH", "ARROWZERO_LOG_PATH",
        "ARROWZERO_REGISTRY_CAPACITY", "ARROWZERO_HOST", "ARROWZERO_PORT",
    ]:
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.registry_capacity == 128
    assert str(s.db_path).endswith("arrowzero.db")
    assert str(s.log_path).endswith(".jsonl")


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("ARROWZERO_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("ARROWZERO_LOG_PATH", str(tmp_path / "x.jsonl"))
    monkeypatch.setenv("ARROWZERO_REGISTRY_CAPACITY", "7")
    monkeypatch.setenv("ARROWZERO_PORT", "9001")
    s = Settings.from_env()
    assert s.registry_capacity == 7
    assert s.port == 9001
    assert s.db_path == tmp_path / "x.db"


def test_bad_capacity_rejected(monkeypatch):
    monkeypatch.setenv("ARROWZERO_REGISTRY_CAPACITY", "0")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_bad_port_rejected(monkeypatch):
    monkeypatch.setenv("ARROWZERO_PORT", "70000")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_settings_frozen():
    s = Settings.from_env()
    with pytest.raises(Exception):
        s.port = 1  # type: ignore[misc]
