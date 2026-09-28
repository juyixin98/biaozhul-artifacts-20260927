"""pytest 夹具：临时 DB / 已构建 FastAPI 客户端 / 规则档。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.main import build_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.rules.parser import load_registry  # noqa: E402


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        rules_path=str(ROOT / "config" / "rules.json"),
        db_path=str(tmp_path / "audit" / "test.sqlite3"),
        audit_key=Fernet.generate_key().decode(),
        audit_token="test-audit-token",
        max_request_chars=100_000,
    )


@pytest.fixture()
def client(settings):
    app = build_app(settings)
    with TestClient(app) as c:
        c.audit_token = settings.audit_token  # type: ignore[attr-defined]
        yield c
    app.state.store.close()


@pytest.fixture()
def registry():
    return load_registry(str(ROOT / "config" / "rules.json"))


@pytest.fixture()
def profile_docs():
    doc = json.loads((ROOT / "config" / "rules.json").read_text("utf-8"))
    return {p["name"]: p for p in doc["profiles"]}
