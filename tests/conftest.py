"""Shared test fixtures.

Every test gets a service backed by a fresh in-memory SQLite database so
runs can never leak state across tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.api import create_app
from app.service import AuditService

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures"


@pytest.fixture
def service() -> AuditService:
    svc = AuditService(":memory:", fixtures_dir=FIXTURES_DIR)
    yield svc
    svc.close()


@pytest.fixture
def client(service: AuditService):
    from fastapi.testclient import TestClient

    app = create_app(db_path=":memory:", fixtures_dir=FIXTURES_DIR)
    # share one service: close both handles carefully
    test_service = app.state.service
    with TestClient(app) as c:
        yield c
    test_service.close()


# Two declared policies used across the tests. The "broken" policy keys
# only on path+query; the "fixed" policy also covers the negotiation and
# identity dimensions.
BROKEN_POLICY = {
    "name": "path-query-only",
    "covered_dimensions": ["path", "query"],
    "identity": {"mode": "auto"},
    "shared": True,
}

# Variant of the broken policy that explicitly claims authenticated
# responses are shareable. This is what produces the cross-identity
# collision counterexample: the kernel no longer fail-safes, it reports.
BROKEN_SHARED_IDENTITY_POLICY = {
    "name": "path-query-only-shared-identity",
    "covered_dimensions": ["path", "query"],
    "identity": {"mode": "shared"},
    "shared": True,
}

FIXED_POLICY = {
    "name": "full-negotiation-and-identity",
    "covered_dimensions": [
        "path",
        "query",
        "accept-language",
        "accept-encoding",
        "accept",
        "authorization",
        "cookie",
    ],
    "identity": {"mode": "per_identity"},
    "shared": True,
}

ALL_FIXTURES = [
    "language",
    "encoding",
    "identity",
    "missing_vary",
    "vary_wildcard",
]
