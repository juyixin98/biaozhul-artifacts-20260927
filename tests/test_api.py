"""HTTP 端到端：真实输入→输出、状态码/错误类别、结果不泄漏真实数据。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import load_fixture


@pytest.fixture()
def client(state):
    app = create_app(state=state)
    with TestClient(app) as c:
        yield c


def test_health_reports_version(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["key_source"] == "ephemeral"


def test_analyze_success_end_to_end(client):
    fx = load_fixture("tiny_patients")
    r = client.post("/analyze", json={**fx, "k": 2, "l": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["failure_code"] is None
    assert {x["column"]: x["level"] for x in body["chosen_levels"]} == {"age": 2, "city": 2}
    assert body["info_loss"] == pytest.approx(0.733333, abs=1e-6)
    assert body["summary"]["n_rows"] == 6
    assert body["null_kept_in_sample"] is True
    assert body["run_id"].startswith("run_")
    assert body["disclaimer"]  # 明确指标局限


def test_response_never_leaks_real_values(client):
    fx = load_fixture("tiny_patients")
    r = client.post("/analyze", json={**fx, "k": 2, "l": 2})
    raw = r.text
    # 真实城市/诊断不得出现在响应任何位置
    for secret in ["Haidian", "Xicheng", "Pudong", "flu", "diabetes", "hypertension"]:
        assert secret not in raw, f"response leaks real value {secret!r}"
    # 只暴露规模/计数类字段
    for ec in r.json()["equivalence_classes"]:
        assert set(ec) == {
            "class_index", "size", "distinct_sensitive", "max_sensitive_frequency",
            "meets_k", "meets_l", "risk_level", "contains_null_qi",
        }


def test_k_unreachable_returns_200_with_explicit_status_and_risk_classes(client):
    """不可达不是 HTTP 崩溃：200 + status=k_unreachable + failure_code。"""
    fx = load_fixture("unique_signatures")
    r = client.post("/analyze", json={**fx, "k": 2, "l": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "k_unreachable"
    assert body["failure_code"] == "K_UNREACHABLE"
    assert body["failure_category"] == "threshold_unreachable"
    assert body["chosen_levels"] is None
    # 风险类仍然输出（只含计数）
    high = [c for c in body["equivalence_classes"] if c["risk_level"] == "high"]
    assert any(c["size"] == 1 for c in high)


def test_l_unreachable_status(client):
    fx = load_fixture("homogeneous_dx")
    r = client.post("/analyze", json={**fx, "k": 2, "l": 2})
    body = r.json()
    assert body["status"] == "l_unreachable"
    assert body["failure_code"] == "L_UNREACHABLE"
    medium = [c for c in body["equivalence_classes"] if c["risk_level"] == "medium"]
    assert len(medium) == 2


def test_validation_error_is_structured_422_not_success(client):
    r = client.post("/analyze", json={"name": "bad", "columns": [], "rows": [], "k": 2})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    assert body["error"]["category"] == "validation"
    assert "validation_errors" in body["error"]["details"]


def test_unknown_column_in_row_is_422_with_specific_code(client):
    fx = load_fixture("tiny_patients")
    payload = {**fx, "k": 2, "l": 2}
    payload["rows"][0]["ghost"] = 1
    r = client.post("/analyze", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNKNOWN_COLUMN"


def test_run_persistence_and_fetch(client):
    fx = load_fixture("tiny_patients")
    # 先存数据集
    rs = client.post("/datasets", json=fx)
    assert rs.status_code == 201
    schema_id = rs.json()["schema_id"]

    # 对已存数据重放
    rr = client.post(f"/datasets/{schema_id}/analyze", json={"k": 2, "l": 2})
    assert rr.status_code == 200
    run_id = rr.json()["run_id"]

    # 取回不可变结果
    rg = client.get(f"/runs/{run_id}")
    assert rg.status_code == 200
    assert rg.json()["run_id"] == run_id
    assert rg.json()["status"] == "succeeded"

    # 未知 run -> 404 明确错误
    assert client.get("/runs/run_does_not_exist").status_code == 404


def test_dataset_metadata_does_not_return_rows(client):
    fx = load_fixture("tiny_patients")
    rs = client.post("/datasets", json=fx)
    schema_id = rs.json()["schema_id"]
    meta = client.get(f"/datasets/{schema_id}").json()
    assert "ciphertext" not in meta
    assert "rows" not in meta
    assert meta["input_fingerprint"]
    assert "flu" not in json.dumps(meta, ensure_ascii=False)


def test_duplicate_submit_is_idempotent_not_an_error(client):
    fx = load_fixture("tiny_patients")
    r1 = client.post("/datasets", json=fx)
    r2 = client.post("/datasets", json=fx)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["schema_id"] == r2.json()["schema_id"]
    assert r1.json()["inserted"] is True
    assert r2.json()["inserted"] is False


def test_list_runs_and_audit(client):
    fx = load_fixture("tiny_patients")
    client.post("/analyze", json={**fx, "k": 2, "l": 2})
    runs = client.get("/runs").json()
    assert runs["count"] >= 1

    audit = client.get("/audit").json()
    statuses = {r["status"] for r in audit["items"]}
    assert "succeeded" in statuses
    # 审计明细不含真实数据
    assert "flu" not in json.dumps(audit, ensure_ascii=False)
