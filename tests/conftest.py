"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.reference import FfmpegReference, PyloudnormReference  # noqa: E402


@pytest.fixture(scope="session")
def ffmpeg() -> FfmpegReference:
    return FfmpegReference()


@pytest.fixture(scope="session")
def pyln_ref() -> PyloudnormReference:
    return PyloudnormReference()


@pytest.fixture()
def client():
    """FastAPI TestClient backed by an isolated in-memory job store."""
    import app.main as main
    from fastapi.testclient import TestClient

    main.init_resources(":memory:")
    with TestClient(main.app) as c:
        yield c
