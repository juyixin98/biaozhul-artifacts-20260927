"""Pytest fixtures: isolated temp DB + seeded app for every test."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.store import VersionStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        unknown_char_cost=20.0,
        close_gap_threshold=1.0,
        max_input_chars=1000,
        seed_on_start=False,  # tests publish deterministic versions themselves
    )


@pytest.fixture
def store(settings: Settings) -> VersionStore:
    s = VersionStore(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def seeded_store(settings: Settings) -> VersionStore:
    s = VersionStore(settings.db_path)
    s.seed_from_json(Path(__file__).resolve().parent.parent / "data" / "seed_lexicon.json")
    yield s
    s.close()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.store.close()


@pytest.fixture
def seeded_client(settings: Settings) -> TestClient:
    # Enable seeding for a realistic end-to-end app.
    seeded = Settings(
        db_path=settings.db_path,
        unknown_char_cost=20.0,
        close_gap_threshold=1.0,
        max_input_chars=1000,
        seed_on_start=True,
    )
    app = create_app(seeded)
    with TestClient(app) as c:
        yield c
    app.state.store.close()
