"""Shared pytest configuration.

Each test session gets a ``run_id`` that is stamped on every service log line
and recorded against assertions, so log output can be correlated to inputs and
to a specific run. Logs are written under ``test-results/<session>/``.
"""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.audit.logging_config import configure_logging  # noqa: E402
from app.config import Settings  # noqa: E402

SESSION_RUN_ID = "test-" + secrets.token_hex(6)


def pytest_configure(config: pytest.Config) -> None:
    results_dir = ROOT / "test-results" / SESSION_RUN_ID
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "session.json").write_text(
        json.dumps({"run_id": SESSION_RUN_ID}, indent=2)
    )
    settings = Settings(  # type: ignore[call-arg]
        db_path=results_dir / "unused.db",
        log_dir=results_dir / "logs",
        log_level="DEBUG",
    )
    logger = configure_logging(settings)
    logger.info("pytest session start run_id=%s", SESSION_RUN_ID)
    config._results_dir = results_dir  # type: ignore[attr-defined]
    config._session_run_id = SESSION_RUN_ID  # type: ignore[attr-defined]


@pytest.fixture()
def run_id(pytestconfig: pytest.Config) -> str:
    return pytestconfig._session_run_id  # type: ignore[attr-defined]


@pytest.fixture()
def results_dir(pytestconfig: pytest.Config) -> Path:
    return pytestconfig._results_dir  # type: ignore[attr-defined]
