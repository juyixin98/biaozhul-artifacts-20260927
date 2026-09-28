"""pytest configuration: isolated settings, service factory, structured logs."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "samples"))

from arrowzero.config import Settings  # noqa: E402
from arrowzero.metadata import MetadataStore  # noqa: E402
from arrowzero.observability import RunLogger  # noqa: E402
from arrowzero.service import Registry, ViewService  # noqa: E402

from helpers import TestLog, make_run_id  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        log_path=tmp_path / "test.jsonl",
        registry_capacity=64,
        host="127.0.0.1",
        port=0,
    )


@pytest.fixture()
def store(settings: Settings) -> MetadataStore:
    s = MetadataStore(settings.db_path)
    yield s
    s.close()


@pytest.fixture()
def run_logger(settings: Settings) -> RunLogger:
    return RunLogger(settings.log_path, echo=False)


@pytest.fixture()
def service(store: MetadataStore, run_logger: RunLogger) -> ViewService:
    return ViewService(store, Registry(64), run_logger, run_purpose="pytest")


@pytest.fixture()
def run_id() -> str:
    return make_run_id("case")


@pytest.fixture()
def test_log(tmp_path: Path) -> TestLog:
    return TestLog(tmp_path / "cases.jsonl")


@pytest.fixture()
def client(settings: Settings):
    from fastapi.testclient import TestClient

    from arrowzero.api.app import create_app

    app = create_app(settings)
    with TestClient(app) as c:
        yield c
