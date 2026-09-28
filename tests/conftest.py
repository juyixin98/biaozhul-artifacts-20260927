"""Shared pytest configuration and fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dictsvc.config import Settings
from dictsvc.api import create_app
from dictsvc.core.model import BatchInput

from .oracle import RefBatch

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_LOG_DIR = REPO_ROOT / "test_results" / "logs"


@pytest.fixture(scope="session", autouse=True)
def session_banner():
    TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
    banner = TEST_LOG_DIR / "session-info.log"
    from dictsvc.config import get_settings
    info = get_settings().version_info()
    with banner.open("w") as f:
        f.write("dictsvc test session\n")
        for k, v in sorted(info.items()):
            f.write(f"{k}: {v}\n")
        f.write(f"python_executable: {sys.executable}\n")
    print("\n=== versions ===")
    for k, v in sorted(info.items()):
        print(f"{k}: {v}")
    print("================")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        sqlite_path=str(tmp_path / "test.db"),
        log_dir=str(tmp_path / "logs"),
        default_target_width=8,
        default_width_policy="reject",
        default_sort_policy="type_then_value",
        log_level="DEBUG",
    )


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(settings):
    from dictsvc.metadata.store import MetadataStore
    return MetadataStore(settings.sqlite_path)


def to_batch_input(ref: RefBatch) -> BatchInput:
    return BatchInput(
        batch_id=ref.batch_id, values=tuple(ref.values),
        indices=tuple(ref.indices), valid=tuple(ref.valid))


@pytest.fixture
def make_payload():
    def _make(ref_batches, **opts):
        return {
            "batches": [
                {
                    "batch_id": b.batch_id,
                    "dictionary": list(b.values),
                    "indices": [None if not ok else i
                                for i, ok in zip(b.indices, b.valid)],
                    "valid": list(b.valid),
                } for b in ref_batches
            ],
            **opts,
        }
    return _make


def assert_error(resp, status: int, category: str):
    """Assert the concrete failure category; never accept a bare success."""
    assert resp.status_code == status, (
        f"expected {status} {category}, got {resp.status_code}: "
        f"{resp.text[:400]}")
    data = resp.json()
    assert data["ok"] is False
    assert data["error"]["category"] == category, data
    return data


@pytest.fixture(autouse=True)
def _correlate(request):
    """Emit identifying prefix so a failing test log ties to the test/run."""
    print(f"\n--- TEST {request.node.nodeid} ---")
    yield
