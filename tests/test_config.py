"""Configuration defaults and environment overrides."""
import os

from app.config import JitterConfig


def test_defaults_pinned():
    cfg = JitterConfig()
    assert cfg.min_delay_ms == 20.0
    assert cfg.max_delay_ms == 120.0
    assert cfg.safety_margin_ms == 2.0
    assert cfg.jitter_multiplier == 8.0
    assert cfg.clock_rate == 8000
    assert cfg.samples_per_packet == 80
    assert cfg.frame_ms == 10.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("JB_MIN_DELAY_MS", "5")
    monkeypatch.setenv("JB_MAX_DELAY_MS", "33")
    monkeypatch.setenv("JB_JITTER_K", "12")
    monkeypatch.setenv("JB_MAX_PACKETS", "64")
    cfg = JitterConfig.from_env()
    assert cfg.min_delay_ms == 5.0
    assert cfg.max_delay_ms == 33.0
    assert cfg.jitter_multiplier == 12.0
    assert cfg.max_buffer_packets == 64


def test_empty_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("JB_MIN_DELAY_MS", "")
    assert JitterConfig.from_env().min_delay_ms == 20.0
