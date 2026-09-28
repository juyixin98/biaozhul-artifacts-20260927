"""Shared pytest fixtures: each test gets an isolated seeded SQLite DB."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import CostModel, Limits, Settings
from app.service import SpellcheckService
from app.storage import VersionStore
from scripts.seed_db import parse_seed_file

UNIT_COSTS = CostModel()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "test.db"),
        seed_file="examples/seed_words.txt",
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789'-",
        limits=Limits(
            max_candidates_scored=5000,
            max_search_nodes=60_000,
        ),
        costs=CostModel(
            substitute={"i:y": 0.5},
            transpose={"e:i": 0.75, "i:e": 0.75},
        ),
    )


@pytest.fixture()
def store(settings) -> VersionStore:
    s = VersionStore(settings.db_path)
    entries = parse_seed_file(settings.seed_file, settings.alphabet_set)
    s.create_version(entries, description="test fixture", activate=True)
    return s


@pytest.fixture()
def service(settings, store) -> SpellcheckService:
    return SpellcheckService(settings, store)


@pytest.fixture()
def client(settings, store) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
