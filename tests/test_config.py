"""Configuration layer tests."""
from __future__ import annotations

import pytest

from dictsvc.config import Settings, get_settings


def test_defaults_are_local_safe(monkeypatch):
    for k in ("DICTSVC_SQLITE_PATH", "DICTSVC_LOG_DIR",
              "DICTSVC_DEFAULT_WIDTH", "DICTSVC_DEFAULT_WIDTH_POLICY"):
        monkeypatch.delenv(k, raising=False)
    s = get_settings()
    assert s.default_target_width == 8
    assert s.default_width_policy == "reject"
    assert s.default_sort_policy == "type_then_value"
    assert s.sqlite_path.endswith("dictsvc.db")


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DICTSVC_SQLITE_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("DICTSVC_DEFAULT_WIDTH", "16")
    monkeypatch.setenv("DICTSVC_DEFAULT_WIDTH_POLICY", "expand")
    s = get_settings()
    assert s.sqlite_path == str(tmp_path / "x.db")
    assert s.default_target_width == 16
    assert s.default_width_policy == "expand"


def test_malformed_width_env_fails_loudly(monkeypatch):
    monkeypatch.setenv("DICTSVC_DEFAULT_WIDTH", "eight")
    with pytest.raises(ValueError, match="not an integer"):
        get_settings()


def test_version_info_reports_stack():
    info = Settings(":memory:", "logs", 8, "reject",
                    "type_then_value", "INFO").version_info()
    assert {"python", "pyarrow", "fastapi", "sqlite", "pytest"} <= set(info)
