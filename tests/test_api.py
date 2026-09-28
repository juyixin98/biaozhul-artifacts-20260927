"""API 端到端测试：断言具体响应字段、失败类别 HTTP 映射、审计关联。"""

from __future__ import annotations

import json

import pytest

from diffanalyzer.api import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(service):
    app = create_app(service, service.store, service.auditor,
                     max_trace_steps=24)
    return TestClient(app)


def _policy(version, *rules):
    return {"version": version, "rules": list(rules)}


def test_health_and_trust_endpoints_expose_semantics(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["default_deny"] is True
    assert body["explicit_deny_precedence"] is True
    trust = client.get("/v1/trust").json()
    assert trust["trusted_submitters"] == ["submitter"]
    assert trust["user_backend"] == "none"


def test_full_diff_flow_over_http_with_concrete_witness(client, sign_policy):
    import uuid
    request_id = "req_test_" + uuid.uuid4().hex
    headers = {"X-Actor": "reviewer-1", "X-Request-Id": request_id}
    old = _policy("v1", {"id": "read", "effect": "ALLOW",
                         "resource_prefix": "logs/", "actions": ["get"],
                         "principals": ["acct/alice"]})
    new = _policy("v2",
                  {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
                   "actions": ["get"], "principals": ["acct/alice"]},
                  {"id": "read2", "effect": "ALLOW",
                   "resource_prefix": "logs/2026/", "actions": ["get"],
                   "principals": ["acct/bob"]})
    r1 = client.post("/v1/policies", json=sign_policy(old), headers=headers)
    r2 = client.post("/v1/policies", json=sign_policy(new), headers=headers)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    r3 = client.post("/v1/diffs", json={
        "old_version": "v1", "new_version": "v2",
        "scope": {"resource_prefixes": ["logs/"], "actions": ["get"]},
    }, headers=headers)
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body["summary"]["verdict"] == "WIDENED"
    assert body["summary"]["default_deny"] is True
    # 具体见证：bob 读 logs/2026/0
    witnesses = body["witnesses"]["widened"]
    assert witnesses, "必须返回新增允许的具体请求见证"
    w = witnesses[0]
    assert w["request"]["principal"] == "acct/bob"
    assert w["request"]["resource"].startswith("logs/2026/")
    assert w["old"]["verdict"] == "DENY" and w["new"]["verdict"] == "ALLOW"

    # 受限空间是显式描述的（区域/主体/操作/属性）
    space = body["restricted_space"]
    anchors = {rg["anchor"] for rg in space["regions"]}
    assert {"logs/", "logs/2026/"}.issubset(anchors)
    assert space["size"] == space["enumerated"]

    # 结果可按 diff_id 取回
    r4 = client.get(f"/v1/diffs/{body['diff_id']}")
    assert r4.status_code == 200
    assert r4.json()["summary"]["verdict"] == "WIDENED"

    # 审计可按请求身份关联，并单列失败/不确定
    assert body["request_id"] == request_id
    r5 = client.get(f"/v1/audit?request_id={request_id}")
    events = r5.json()["events"]
    assert any(e["component"] == "crypto_verify" for e in events)
    assert all(e["actor"] == "reviewer-1" for e in events)


def test_unsigned_policy_rejected_with_class(client):
    r = client.post("/v1/policies", json={
        "submitted_by": "submitter",
        "signed_at_claim": {"type": "policy-submission/v1",
                            "submitted_by": "submitter", "version": "v1",
                            "policy": {"version": "v1", "rules": []}}
        # 无 signature
    })
    assert r.status_code == 422
    body = r.json()["failure"]
    assert body["kind"] == "CRYPTO_MISSING_SIGNATURE"


def test_schema_failure_is_400_with_class_and_detail(client, sign_policy):
    env = sign_policy({"version": "v1", "rules": [
        {"id": "r", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"],
         "surprise": 1}]})
    r = client.post("/v1/policies", json=env)
    assert r.status_code == 400
    assert r.json()["failure"]["kind"] == "SCHEMA_INVALID"
    assert "surprise" in str(r.json()["failure"]["details"])


def test_unknown_condition_yields_unsure_not_allow_over_http(client,
                                                             sign_policy):
    client.post("/v1/policies", json=sign_policy(
        {"version": "v1", "rules": []}))
    client.post("/v1/policies", json=sign_policy(
        {"version": "v2", "rules": [
            {"id": "put", "effect": "ALLOW", "resource_prefix": "tmp/",
             "actions": ["put"], "principals": ["*"], "anonymous": True,
             "conditions": [{"attribute": "locked", "op": "NotEq",
                             "value": True}]}]}))
    r = client.post("/v1/diffs", json={
        "old_version": "v1", "new_version": "v2",
        "scope": {"resource_prefixes": ["tmp/"], "actions": ["put"]}})
    body = r.json()
    assert body["summary"]["verdict"] == "WIDENED_WITH_UNKNOWN"
    assert body["summary"]["has_uncertain_points"] is True
    assert body["summary"]["new_unknown_requests"] > 0
    # 解释文本明确“不确定不按允许处理”
    assert "UNKNOWN" in body["interpretation"]["uncertainty_policy"]


def test_audit_separates_inconclusive_events(client, sign_policy):
    client.post("/v1/policies", json=sign_policy(
        {"version": "v1", "rules": []}))
    client.post("/v1/policies", json=sign_policy(
        {"version": "v2", "rules": [
            {"id": "put", "effect": "ALLOW", "resource_prefix": "tmp/",
             "actions": ["put"], "principals": ["*"], "anonymous": True,
             "conditions": [{"attribute": "locked", "op": "NotEq",
                             "value": True}]}]}))
    body = client.post("/v1/diffs", json={
        "old_version": "v1", "new_version": "v2",
        "scope": {"resource_prefixes": ["tmp/"], "actions": ["put"]}}).json()
    audit = client.get(
        f"/v1/audit?request_id={body['request_id']}").json()
    assert audit["inconclusive"], "不确定结论必须单列"
    assert audit["failures"] == []
