"""Pytest configuration: make src/ importable and share fixture loaders."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reorgindex.app import Application  # noqa: E402
from reorgindex.config import Settings  # noqa: E402


def load_recording(scenario: str) -> dict:
    return json.loads((ROOT / "fixtures" / scenario / "recording.json").read_text())


def load_expected(scenario: str) -> dict:
    return json.loads((ROOT / "fixtures" / scenario / "expected.json").read_text())


def producer_address(recording: dict) -> str:
    return recording["producer_address"]


def settings_for(db_path: Path, recording: dict) -> Settings:
    return Settings(
        database_path=db_path,
        finality_depth=int(recording["finality_depth"]),
        allowed_difficulties=frozenset({4, 16}),
        service_name="test",
        log_level="ERROR",
    )


@pytest.fixture
def short_fork_docs():
    return load_recording("short_fork"), load_expected("short_fork")


@pytest.fixture
def deep_fork_docs():
    return load_recording("deep_fork"), load_expected("deep_fork")


@pytest.fixture
def interrupt_docs():
    return load_recording("interrupt"), load_expected("interrupt")


@pytest.fixture
def make_app(tmp_path):
    apps: list[Application] = []

    def _make(recording: dict, *, name: str = "test.db", log_level: str = "ERROR"):
        settings = Settings(
            database_path=tmp_path / name,
            finality_depth=int(recording["finality_depth"]),
            allowed_difficulties=frozenset({4, 16}),
            service_name="test",
            log_level=log_level,
        )
        app = Application(settings, {recording["producer_address"]})
        apps.append(app)
        return app

    yield _make

    for app in apps:
        app.close()
