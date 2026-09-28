"""配置层独立测试：TOML 默认值、环境变量覆盖、未知键拒绝。"""

from __future__ import annotations

import pytest


def test_defaults_load():
    from anon_risk.config import load_settings
    s = load_settings()
    assert s.app.metric_version == "1.0.0"
    assert s.kernel.lattice_combo_cap == 10_000


def test_env_override(monkeypatch):
    from anon_risk import config as cfg
    monkeypatch.setenv("ANON_RISK_KERNEL__LATTICE_COMBO_CAP", "42")
    monkeypatch.setenv("ANON_RISK_LOGGING__LEVEL", "DEBUG")
    monkeypatch.setenv("ANON_RISK_SECURITY__ALLOW_EPHEMERAL_KEY", "false")
    s = cfg.load_settings()
    assert s.kernel.lattice_combo_cap == 42
    assert s.logging.level == "DEBUG"
    assert s.security.allow_ephemeral_key is False


def test_bad_bool_env(monkeypatch):
    from anon_risk import config as cfg
    monkeypatch.setenv("ANON_RISK_SECURITY__ALLOW_EPHEMERAL_KEY", "maybe")
    with pytest.raises(ValueError):
        cfg.load_settings()


def test_unknown_config_key_rejected(tmp_path, monkeypatch):
    from anon_risk import config as cfg
    bad = tmp_path / "bad.toml"
    bad.write_text("[kernel]\nlattice_combo_cap = 1\nnonexistent = 2\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="未知键"):
        cfg.load_settings(bad)
