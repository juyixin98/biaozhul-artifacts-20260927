"""Pytest fixtures: isolated temp DB, fresh app, authenticated client."""
from __future__ import annotations

import os
import tempfile

import pytest

# Ephemeral audit DB per test run; key generated for the process.
_TMP = tempfile.mkdtemp(prefix="logsafe-test-")
os.environ.setdefault("LOGSAFE_DB", os.path.join(_TMP, "audit-test.sqlite3"))
os.environ.setdefault("LOGSAFE_AUDIT_KEY", "test-audit-key")

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "audit.sqlite3"),
        fernet_key=_fernet(),
        audit_key="test-audit-key",
        default_profile="standard",
    )


def _fernet() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


@pytest.fixture()
def app(settings):
    application = create_app(settings)
    yield application
    application.state.audit.close()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        c.headers_update = lambda **kw: c.headers.update(kw)  # convenience
        yield c


@pytest.fixture()
def auth_headers():
    return {"X-Audit-Key": "test-audit-key"}
