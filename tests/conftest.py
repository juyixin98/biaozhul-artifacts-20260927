"""Shared pytest configuration.

Each test gets an isolated workspace root and audit database under a tmp
directory, so no test can read or mutate another test's state. The sentinel
helpers let tests prove that rejected runs leave *nothing* outside the run's
own isolated directory.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit.audit import AuditDB
from app.config import Budgets, Policy, Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        version="test-1.0.0",
        workspace_root=tmp_path / "workspace",
        audit_db=tmp_path / "audit" / "audit.db",
        max_upload_bytes=2 * 1024 * 1024,
        budgets=Budgets(
            max_total_uncompressed_bytes=64 * 1024,
            max_file_size_bytes=32 * 1024,
            max_entries=20,
            max_depth=6,
            max_compression_ratio=50,
            symlink_resolution_steps=40,
        ),
        policy=Policy(allow_case_collisions=False, allow_symlinks=True),
    )


@pytest.fixture
def audit(settings: Settings) -> AuditDB:
    db = AuditDB(settings.audit_db, version=settings.version)
    yield db
    db.close()


@pytest.fixture
def app(settings: Settings):
    application = create_app(settings)
    yield application
    application.state.audit.close()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def service(settings: Settings, audit: AuditDB):
    from app.service import GuardService

    return GuardService(settings, audit)


# ---------------------------------------------------------------------------
# Filesystem snapshot helpers — used to prove "nothing changed outside".
# ---------------------------------------------------------------------------
def tree_signature(root: Path) -> dict:
    """Map of every path under root to (type, size, content-digest-ish)."""
    sig = {}
    if not root.exists():
        return sig
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            sig[rel] = ("symlink", os.readlink(p))
        elif p.is_dir():
            sig[rel] = ("dir", None)
        else:
            sig[rel] = ("file", p.stat().st_size, p.read_bytes()[:8])
    return sig


@pytest.fixture
def sentinel_outside(tmp_path, settings) -> Path:
    """A canary file OUTSIDE the workspace root; must never be touched."""
    canary = tmp_path / "canary.txt"
    canary.write_bytes(b"do-not-touch")
    sibling = tmp_path / "canary_sibling.txt"
    sibling.write_bytes(b"also-untouched")
    return tmp_path
