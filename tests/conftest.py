"""共享 pytest 夹具：每个测试独立临时仓库 + ASGI TestClient。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from fixtures import load_all  # noqa: E402


@pytest.fixture
def client(tmp_path):
    cfg = Config.from_env(str(tmp_path / "warehouse"))
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(client):
    return client.app.state.store


@pytest.fixture(params=load_all(), ids=[s.name for s in load_all()])
def scenario(request):
    return request.param
