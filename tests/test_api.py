"""HTTP 接口层测试：FastAPI + TestClient，验证请求身份关联与审计可追溯。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pruning.api import create_app
from pruning.config import Config
from tools import make_fixtures


@pytest.fixture()
def client(tmp_path):
    root = str(tmp_path / "data")
    make_fixtures.build(root)
    cfg = Config(data_root=root, db_path=str(tmp_path / "cat.sqlite"))
    app = create_app(cfg)
    return TestClient(app)


def test_healthz_and_versions(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["versions"]["timezone"] == "UTC"
    assert body["versions"]["date_transform"] == "dateconv-1.0.0"


def test_register_plan_validate_flow(client):
    r = client.post("/register", json={
        "table": "events",
        "truncated_string_columns": ["region"],
        "truncate_prefix_len": 4})
    assert r.status_code == 200
    assert r.json()["files"] == 6

    payload = {"table": "events", "request_id": "req-http-1", "predicates": [
        {"column": "event_ts", "kind": "range",
         "lower": "2024-02-01", "upper": "2024-02-29", "upper_inclusive": True}]}
    v = client.post("/validate", json=payload).json()
    assert v["status"] == "pass"
    assert v["zero_missed_matches"] is True
    assert v["request_id"] == "req-http-1"
    assert len(v["kernel_selected_files"]) == 2

    # 审计按 request_id 可追溯，且决策带原因码与层级
    audit = client.get("/requests/req-http-1").json()
    assert audit["table"] == "events"
    reasons = [d["reason"] for d in audit["decisions"]]
    assert "partition_outside_range" in reasons
    assert all(d["detail"] for d in audit["decisions"])


def test_pruned_decisions_carry_impossibility_reason(client):
    client.post("/register", json={"table": "events"})
    r = client.post("/plan", json={"table": "events", "predicates": [
        {"column": "event_ts", "kind": "range",
         "lower": "2024-03-01", "upper": "2024-03-31", "upper_inclusive": True}]}).json()
    pruned = [d for d in r["decisions"] if d["certainty"] == "pruned"
              and d["layer"] == "partition"]
    assert pruned
    for d in pruned:
        # 每个被裁分区都给出"不可能匹配"的具体理由和证据
        assert d["reason"] == "partition_outside_range"
        assert d["detail"]
        assert d["evidence"].get("candidate_first") or d["evidence"].get("candidate_last")
    # 不确定性单列字段存在
    assert "uncertain_notes" in r


def test_unknown_table_returns_404(client):
    r = client.post("/plan", json={"table": "ghost", "predicates": []})
    assert r.status_code == 404


def test_unknown_column_flagged(client):
    client.post("/register", json={"table": "events"})
    r = client.post("/validate", json={"table": "events", "predicates": [
        {"column": "ghost_col", "kind": "is_null"}]}).json()
    assert r["status"] == "fail"
    assert any(f["category"] == "unknown_column" for f in r["failures"])
