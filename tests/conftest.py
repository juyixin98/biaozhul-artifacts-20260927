"""Shared pytest fixtures: independent config and temp data roots."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from clockalign.config import load_config  # noqa: E402


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOCKALIGN_HOME", str(tmp_path / "home"))
    return load_config()
