"""端到端 HTTP 行为测试：具体结果断言、风险类、不可达、失败类别而非 200。"""

from __future__ import annotations

import json

from .conftest import ADMIN_HEADERS
from . import expected


def test_create_run_returns_token_and_null_evidence(client, tiny_payload):
    r = client.post("/runs", json=tiny_payload)
    assert r.status_code == 201
    body = r.json()
    assert body["row_count"] == 6
    assert body["metric_version"] == "1.0.0"
    assert set(body["quasi_identifiers"]) == {"zip", "age"}
    # 层级校验证据随创建返回（聚合，无原始值）
    assert [v["column"] for v in body["hierarchy_validation"]] == ["zip", "age"]
    assert body["hierarchy_validation"][0]["observed_non_null_values"] == 6


def test_evaluate_baseline_has_six_singleton_high_classes(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/evaluate?k=2&l=2",
                    json={"levels": {"zip": 0, "age": 0}},
                    headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["k_anonymized"] is False
    assert body["l_diverse"] is False
    assert body["risk_overall"] == "HIGH"
    assert body["rows_in_violating_classes"] == 6
    assert body["class_size_distribution"] == {"1": 6}  # 6 个单例类各含 1 行
    assert body["worst_prosecutor_risk"] == 1.0
    assert len(body["classes"]) == 6
    assert all(c["risk_category"] == "HIGH" for c in body["classes"])
    # 免责声明在场
    assert "不构成完整隐私保证" in body["disclaimer"]


def test_evaluate_at_optimum_matches_hand_expected(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/evaluate?k=2&l=2",
                    json={"levels": {"zip": 1, "age": 2}},
                    headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["k_anonymized"] is True and body["l_diverse"] is True
    assert body["class_size_distribution"] == {"3": 6}  # 2 类各 3 行 => 6 行
    assert body["discernibility"] == expected.TINY_OPTIMUM_K2_L2["discernibility"]
    assert body["loss_metric"] == expected.TINY_OPTIMUM_K2_L2["loss_metric"]
    # 两个类的敏感分布：一种病 2 次、一种病 1 次（hist 不暴露病名）
    for cls in body["classes"]:
        assert cls["sensitive_distinct_non_null"] == 2
        assert cls["sensitive_frequency_histogram"] == {"1": 1, "2": 1}


def test_suggest_endpoint_optimum(client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 2},
                    headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "FEASIBLE"
    assert body["levels"] == {"zip": 1, "age": 2}
    assert body["class_sizes"] == [3, 3]
    assert body["discernibility"] == 18
    assert body["evaluated_vectors"] == 12
    assert any("exhaustive_enumeration" in b for b in body["verdict_basis"])


def test_suggest_unreachable_l_is_explicit_status_not_error(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 3},
                    headers=auth_headers)
    assert r.status_code == 200          # 不是异常
    body = r.json()
    assert body["status"] == "UNREACHABLE"
    assert body["feasible"] is False
    assert body["unreachable_evidence"]["distinct_non_null_sensitive_values"] == 2
    assert any("lt_l_3" in b for b in body["verdict_basis"])


def test_suggest_unreachable_k_exceeds_rows(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/suggest", json={"k": 7, "l": 1},
                    headers=auth_headers)
    body = r.json()
    assert body["status"] == "UNREACHABLE"
    assert body["unreachable_evidence"]["row_count"] == 6


def test_validation_error_has_specific_code_not_generic_success(
        client, tiny_payload):
    bad = json.loads(json.dumps(tiny_payload))
    bad["rows"][0] = ["only", "two"]    # 行宽不符
    r = client.post("/runs", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ROW_WIDTH_MISMATCH"


def test_bad_level_request_returns_specific_code(
        client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/evaluate?k=2&l=2",
                    json={"levels": {"zip": 99}},
                    headers=auth_headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "HIERARCHY_LEVEL_NOT_FOUND"


def test_invalid_k_rejected(client, created_run, auth_headers):
    rid = created_run["run_id"]
    r = client.post(f"/runs/{rid}/evaluate?k=0&l=2",
                    json={"levels": {"zip": 0}},
                    headers=auth_headers)
    assert r.status_code == 422  # pydantic 查询参数校验


def test_unknown_run_404(client, auth_headers):
    r = client.post("/runs/does-not-exist/suggest", json={"k": 2, "l": 2},
                    headers=auth_headers)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_health_and_version(client):
    h = client.get("/health").json()
    assert h["status"] == "ok"
    assert h["key_ephemeral"] is False     # conftest 配置了固定主密钥
    v = client.get("/version").json()
    assert v["metric_version"] == "1.0.0"


def test_correlation_id_roundtrip_and_custom_value(client):
    r = client.get("/health", headers={"X-Correlation-ID": "my-trace-1"})
    assert r.headers["X-Correlation-ID"] == "my-trace-1"
    r2 = client.get("/health")
    assert r2.headers["X-Correlation-ID"].startswith("req-")


def test_csv_upload_endpoint_creates_run(client):
    csv_bytes = (
        "zip,age,disease\n"
        "10001,23,Flu\n10002,25,Cold\n10003,31,Flu\n"
        "12001,35,Cold\n12002,40,Flu\n12003,42,Cold\n"
    ).encode()
    hier = json.dumps({
        "zip": {"levels": [
            {"rule": "prefix", "keep": 4},
            {"rule": "prefix", "keep": 2},
            {"rule": "map", "mapping": {"10": "1x", "12": "1x"}}]},
        "age": {"levels": [
            {"rule": "range", "bins": [0, 30, 50, 200],
             "labels": ["<30", "30-49", "50+"]},
            {"rule": "range", "bins": [0, 120], "labels": ["any"]}]},
    })
    r = client.post(
        "/runs/csv",
        files={"file": ("tiny.csv", csv_bytes, "text/csv")},
        data={"quasi_identifiers": "zip,age",
              "sensitive": "disease",
              "hierarchies": hier},
    )
    assert r.status_code == 201, r.text
    assert r.json()["row_count"] == 6


def test_audit_records_validation_failure(client, tiny_payload, settings):
    bad = json.loads(json.dumps(tiny_payload))
    bad["quasi_identifiers"] = ["nope"]
    r = client.post("/runs", json=bad)
    assert r.status_code == 400
    events = client.get("/audit/events?status=VALIDATION_ERROR",
                        headers=ADMIN_HEADERS).json()
    assert events["total"] >= 1
    assert events["events"][0]["error_code"] == "COLUMN_NOT_FOUND"
