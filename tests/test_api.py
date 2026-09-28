"""FastAPI 验证接口测试：审计、查询、请求标识、脱敏诊断。"""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from colstats.api import create_app
from colstats.config import Config, ServiceConfig, AuditConfig, LogConfig


@pytest.fixture()
def client(tmp_path):
    cfg = Config(
        service=ServiceConfig(db_path=str(tmp_path / "api.db")),
        audit=AuditConfig(expose_values=False),
        log=LogConfig(level="WARNING"),
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_request_id_propagated(client):
    r = client.get("/health", headers={"X-Request-ID": "rid-1234"})
    assert r.headers["X-Request-ID"] == "rid-1234"


def test_audit_good_file(client):
    r = client.post("/audits", json={
        "path": "tests/fixtures/good_stats/data.parquet"
    }, headers={"X-Request-ID": "req-good"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "ACCEPTED"
    assert body["request_id"] == "req-good"
    assert "audit_id" in body


def test_audit_wrong_file_has_locators_and_codes(client):
    r = client.post("/audits", json={
        "path": "tests/fixtures/wrong_stats/data.parquet",
        "expose_values": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "REJECTED"
    codes = {f["code"] for f in body["findings"]}
    assert "MIN_MISMATCH" in codes
    # 定位包含 row_group/column/page
    located = [f for f in body["findings"] if f["code"] == "MIN_MISMATCH"]
    assert all("column" in f["locator"] for f in located)
    assert set(body["summary"]["untrusted_columns"]) == {"code", "id"}


def test_redaction_by_default(client):
    # 默认不暴露真实值：证据里只能出现脱敏结构
    r = client.post("/audits", json={
        "path": "tests/fixtures/wrong_stats/data.parquet"
    })
    mismatches = [f for f in r.json()["findings"] if f["code"] == "MIN_MISMATCH"]
    assert mismatches
    for f in mismatches:
        obs = f["observed"]
        assert obs is not None
        assert "redacted" in obs or "sha256_12" in obs
        assert "value" not in obs


def test_get_audit_by_id_and_request(client):
    r1 = client.post("/audits", json={
        "path": "tests/fixtures/all_null/data.parquet"
    }, headers={"X-Request-ID": "shared-rid"})
    aid = r1.json()["audit_id"]
    r2 = client.get(f"/audits/{aid}")
    assert r2.status_code == 200
    assert r2.json()["verdict"] == "ACCEPTED"
    r3 = client.get("/audits/by-request/shared-rid")
    assert any(a["audit_id"] == aid for a in r3.json()["audits"])


def test_query_wrong_stats_returns_correct_results(client):
    # 坏统计禁用剪枝后查询仍正确（接口层）
    r = client.post("/query", json={
        "path": "tests/fixtures/wrong_stats/data.parquet",
        "column": "id", "predicate": "eq", "value": 500,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["trusted"] is False
    assert body["num_results"] == 1
    assert body["results"] == [500]
    # 不可信 -> 所有行组强制扫描
    assert all(g["chunk_decision"] == "UNDECIDABLE" for g in body["groups"])


def test_query_good_stats_prunes(client):
    r = client.post("/query", json={
        "path": "tests/fixtures/good_stats/data.parquet",
        "column": "id", "predicate": "eq", "value": 700000,
    })
    body = r.json()
    assert body["trusted"] is True
    assert body["num_results"] == 0
    assert all(g["chunk_decision"] == "PRUNE" for g in body["groups"])


def test_prune_check_endpoint(client):
    r = client.post("/prune/check", json={
        "min_claim": "alph", "max_claim": "b",
        "physical_type": "BYTE_ARRAY", "predicate": "eq",
        "value": "alph", "min_truncated": True, "max_truncated": True,
        "trusted": True,
    })
    assert r.json()["decision"] == "UNDECIDABLE"

    r2 = client.post("/prune/check", json={
        "min_claim": 10, "max_claim": 20, "physical_type": "INT32",
        "predicate": "eq", "value": 99, "trusted": True,
    })
    assert r2.json()["decision"] == "PRUNE"

    r3 = client.post("/prune/check", json={
        "min_claim": 10, "max_claim": 20, "physical_type": "INT32",
        "predicate": "eq", "value": 99, "trusted": False,
    })
    assert r3.json()["decision"] == "UNDECIDABLE"


def test_audit_missing_file_404(client):
    r = client.post("/audits", json={"path": "/no/such/file.parquet"})
    assert r.status_code == 404
