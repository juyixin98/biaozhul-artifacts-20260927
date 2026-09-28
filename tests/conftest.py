"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlguard.config import Settings  # noqa: E402
from sqlguard.isolation import snapshot_schema  # noqa: E402
from sqlguard.kernel import Kernel  # noqa: E402
from sqlguard.policy import load_policy  # noqa: E402


@pytest.fixture(scope="session")
def fixture_db() -> Path:
    db = ROOT / "fixtures" / "shop.db"
    if not db.exists():
        import subprocess
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_fixture.py")],
            check=True,
        )
    return db


@pytest.fixture(scope="session")
def policy():
    return load_policy(ROOT / "configs" / "policy.json")


@pytest.fixture(scope="session")
def schema(fixture_db):
    return snapshot_schema(fixture_db)


@pytest.fixture()
def kernel(policy, schema):
    return Kernel(policy, schema)


@pytest.fixture()
def settings(tmp_path, fixture_db):
    return Settings(
        policy_path=str(ROOT / "configs" / "policy.json"),
        fixture_db=str(fixture_db),
        audit_db=str(tmp_path / "audit.db"),
        audit_key=str(tmp_path / "audit.key"),
        host="127.0.0.1",
        port=8080,
    )
