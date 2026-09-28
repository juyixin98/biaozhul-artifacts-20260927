"""集成测试夹具：临时 SQLite + TestClient + 固定测试签名密钥。"""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.coding.signing import generate_private_key, private_key_to_hex
from app.main import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    db = tmp_path / "test_smt.db"
    return Settings(
        db_path=str(db),
        signing_key_path=str(tmp_path / "nonexistent.pem"),
        signing_key_hex=private_key_to_hex(generate_private_key()),
        key_len=32,
        depth=256,
        host="127.0.0.1",
        port=8080,
        log_redact=True,
    )


@pytest.fixture
def client(settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.service.store.close()


@pytest.fixture
def public_key_pem(settings) -> bytes:
    from app.coding.signing import private_key_from_hex, public_key_to_pem
    return public_key_to_pem(private_key_from_hex(settings.signing_key_hex).public_key())
