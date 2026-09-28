"""Shared pytest fixtures: isolated DB, deterministic run identity, log capture.

Every test gets its own SQLite file under a temp directory (state isolation)
and a run id derived from the test node id, so every line in tests/logs can be
filtered back to exactly one test and its inputs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.observability import configure_logging  # noqa: E402
from app.storage.db import Database  # noqa: E402

LOG_DIR = ROOT / "tests" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TEST_LOG = LOG_DIR / "tests.jsonl"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "audit_test.db"),
        audit_log_path=str(TEST_LOG),
        salt_bytes=16,
    )


@pytest.fixture()
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture()
def run_id(request: pytest.FixtureRequest) -> str:
    return f"run::{request.node.nodeid}"


@pytest.fixture(autouse=True)
def _trace(request: pytest.FixtureRequest, settings: Settings):
    """Write a start/progress/verdict marker per test to the correlated JSONL log."""
    configure_logging(settings.audit_log_path)
    import logging

    logger = logging.getLogger("audit_commitments")
    rid = f"run::{request.node.nodeid}"
    logger.info("TEST START", extra={"run_id": rid, "step": "pytest.start",
                                      "verdict": "RUNNING",
                                      "detail": {"db": settings.db_path}})
    yield
    logger.info("TEST END", extra={"run_id": rid, "step": "pytest.end",
                                    "verdict": "FINISHED"})


@pytest.fixture()
def app(settings: Settings):
    application = create_app(settings, configure_logs=False)
    yield application
    application.state.db.close()


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def log_lines_for_run(run_id):
    def _read(rid: str | None = None) -> list[dict]:
        rid = rid or run_id
        if not TEST_LOG.exists():
            return []
        rows = []
        for line in TEST_LOG.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("run_id") == rid:
                rows.append(row)
        return rows

    return _read
