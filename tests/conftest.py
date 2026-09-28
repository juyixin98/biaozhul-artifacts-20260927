"""pytest 公共夹具：每个测试独立临时数据目录、固定主密钥/管理令牌。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# 测试前固定密钥环境（保证测试间可解密、且与“临时密钥”分支分开测）
_FERNET_KEY = "moKe5iTE4kfu5eihqtKji3GLoQLz_tT3YVzcMj4cF98="
os.environ.setdefault("ANON_RISK_MASTER_KEY", _FERNET_KEY)
os.environ.setdefault("ANON_RISK_ADMIN_TOKEN", "test-admin-token")

from anon_risk.api.main import create_app  # noqa: E402
from anon_risk.config import load_settings  # noqa: E402


@pytest.fixture()
def settings(tmp_path):
    os.environ["ANON_RISK_STORAGE__DATA_DIR"] = str(tmp_path / "data")
    os.environ["ANON_RISK_STORAGE__AUDIT_DB"] = str(tmp_path / "audit.db")
    os.environ["ANON_RISK_STORAGE__LOG_DIR"] = str(tmp_path / "logs")
    s = load_settings()
    return s


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def log_file(settings):
    return Path(settings.storage.log_dir) / "anon-risk.log.jsonl"


@pytest.fixture()
def tiny_payload():
    return json.loads((ROOT / "fixtures" / "tiny.json").read_text(encoding="utf-8"))


@pytest.fixture()
def created_run(client, tiny_payload):
    r = client.post("/runs", json=tiny_payload)
    assert r.status_code == 201, r.text
    body = r.json()
    return {"run_id": body["run_id"], "token": body["access_token"], "body": body}


@pytest.fixture()
def auth_headers(created_run):
    return {"X-Run-Token": created_run["token"]}


ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}
