"""共享测试工具:夹具加载与临时应用实例。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def all_fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))


@pytest.fixture(params=all_fixture_names(), ids=all_fixture_names())
def merge_fixture(request) -> dict:
    return load_fixture(request.param)


@pytest.fixture()
def app(tmp_path):
    from app.api import create_app
    from app.config import Settings

    settings = Settings(
        db_path=str(tmp_path / "test.sqlite3"),
        log_path=str(tmp_path / "diagnostics.log"),
    )
    return create_app(settings)


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)
