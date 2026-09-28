"""HTTP API 端到端测试：断言具体状态码、结果与失败类别。"""
from __future__ import annotations

import base64


def test_health_and_issue_flow(client):
    assert client.get("/health").json()["status"] == "ok"
    resp = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"api-secret").decode(),
        "threshold": 2, "share_count": 3,
        "labels": {"1": "alpha-local", "2": "bravo-local"},
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["threshold"] == 2 and body["share_count"] == 3
    assert body["field"] == {"bits": 8, "generator": 283}
    assert len(body["shares"]) == 3
    # 响应里不能回吐原秘密
    assert "api-secret" not in resp.text
    assert client.get(f"/sets/{body['set_id']}").status_code == 200


def test_recover_ok_through_http(client):
    issue = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"http-recover").decode(),
        "threshold": 2, "share_count": 3,
    }).json()
    resp = client.post("/recover", json={"shares": issue["shares"][:2]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "ACCEPTED"
    assert base64.b64decode(body["secret_b64"]) == b"http-recover"
    assert body["diagnostics"]["distinct_x_count"] == 2


def test_recover_below_threshold_http_403_with_category(client):
    issue = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"nope").decode(),
        "threshold": 3, "share_count": 5,
    }).json()
    resp = client.post("/recover", json={"shares": issue["shares"][:2]})
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["category"] == "BELOW_THRESHOLD"
    assert detail["diagnostics"]["distinct_x_count"] == 2


def test_mixed_set_http_422(client):
    a = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"AAAA").decode(),
        "threshold": 2, "share_count": 3}).json()
    b = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"BBBB").decode(),
        "threshold": 2, "share_count": 3}).json()
    resp = client.post("/recover", json={"shares": [a["shares"][0], b["shares"][0]]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["category"] == "MIXED_SET"


def test_unknown_set_http_404(client):
    issue = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"XXXX").decode(),
        "threshold": 2, "share_count": 3}).json()
    for env in issue["shares"][:2]:
        env["set_id"] = "does-not-exist"
    resp = client.post("/recover", json={"shares": issue["shares"][:2]})
    assert resp.status_code == 404
    assert resp.json()["detail"]["category"] == "UNKNOWN_SET"


def test_malformed_evidence_http_422(client):
    issue = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"YYYY").decode(),
        "threshold": 2, "share_count": 3}).json()
    good = issue["shares"][0]
    resp = client.post("/recover", json={"shares": [{"x": 1}, good]})
    # 一个结构坏 + 一个合法：只有 1 个可用 -> BELOW_THRESHOLD(403)
    assert resp.status_code == 403
    assert resp.json()["detail"]["diagnostics"]["malformed"]
    resp2 = client.post("/recover", json={"shares": [{"x": 1}, {"nope": True}]})
    assert resp2.status_code == 422
    assert resp2.json()["detail"]["category"] == "MALFORMED_EVIDENCE"


def test_validation_error_on_bad_issue_input(client):
    resp = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"z").decode(),
        "threshold": 5, "share_count": 3})
    assert resp.status_code == 422
    resp = client.post("/sets", json={
        "secret_b64": "@@@", "threshold": 2, "share_count": 3})
    assert resp.status_code == 422


def test_audit_endpoint_filters_by_request_and_redacts(client):
    issue = client.post("/sets", json={
        "secret_b64": base64.b64encode(b"audited-secret-zz").decode(),
        "threshold": 2, "share_count": 3}).json()
    client.post("/recover", json={"shares": issue["shares"]})
    audit = client.get("/audit", params={"set_id": issue["set_id"]}).json()["records"]
    assert audit
    blob = repr(audit)
    assert "audited-secret-zz" not in blob  # 秘密明文不落审计
    assert all("detail" in r for r in audit)
    # 只有指纹字段引用份额
    assert any("fingerprint" in str(r["detail"]) for r in audit)
