"""Shared pytest fixtures: isolated DB, app and HTTP client per test."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def settings(tmp_path):
    db = tmp_path / "test_ac.db"
    return Settings(
        db_path=str(db),
        secret="test-secret",
        max_patterns=5000,
        max_pattern_bytes=65536,
        max_chunk_bytes=1 << 20,
        default_page_limit=4,   # small: force pagination in ordinary tests
        max_page_limit=100,
    )


@pytest.fixture()
def app(settings):
    application = create_app(settings)
    yield application
    application.state.container.shutdown()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def container(app):
    return app.state.container
