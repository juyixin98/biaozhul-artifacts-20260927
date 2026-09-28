"""FastAPI 接口：fetch 入口、审计查询/签名校验、HTTP 状态码映射、运行隔离。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditStore
from app.webapi import KernelFactory, create_app


@pytest.fixture()
def client(demo_http, zones_resolver, base_policy, tmp_path):
    audit = AuditStore(tmp_path / "audit")
    port = demo_http.port
    zones_resolver.add_zone("demo.local", {"records": ["127.0.0.1"]})
    factory = KernelFactory(
        resolver=zones_resolver,
        base_policy=base_policy,
        audit=audit,
        defaults={"max_redirects": 5, "max_bytes": 1 << 20, "timeout_s": 5.0},
    )
    app = create_app(kernel_factory=factory)
    with TestClient(app) as c:
        c.audit = audit  # type: ignore[attr-defined]
        c.demo_port = port  # type: ignore[attr-defined]
        yield c
    audit.close()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_fetch_success_200(client):
    body = {
        "url": f"http://demo.local:{client.demo_port}/ok",
        "grants": [{
            "id": "g-web", "host": "demo.local", "scheme": "http",
            "port": client.demo_port, "records": ["127.0.0.1"],
        }],
    }
    resp = client.post("/v1/fetch", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "allow"
    assert data["response"]["status"] == 200
    assert data["run_id"]


def test_fetch_policy_denied_403_with_chain(client):
    resp = client.post("/v1/fetch", json={"url": "http://rebind-meta-then-public.test/"})
    assert resp.status_code == 403
    data = resp.json()
    assert data["verdict"] == "deny"
    assert data["failure"]["kind"] == "policy_denied"
    assert data["failure"]["reason"] == "ip.blocked"
    # 决策链随响应返回
    assert data["decision_chain"][0]["stage"] == "run_start"
    assert data["decision_chain"][-1]["reason"] == "ip.blocked"


def test_fetch_input_error_400(client):
    resp = client.post("/v1/fetch", json={"url": "http://user@x/"})
    assert resp.status_code == 400
    assert resp.json()["failure"]["reason"] == "userinfo.forbidden"


def test_fetch_state_conflict_409(client):
    resp = client.post("/v1/fetch", json={
        "url": f"http://demo.local:{client.demo_port}/loop-a",
        "grants": [{"host": "demo.local", "scheme": "http",
                    "port": client.demo_port, "records": ["127.0.0.1"]}],
    })
    assert resp.status_code == 409
    assert resp.json()["failure"]["reason"] == "redirect.loop"


def test_fetch_resource_exhausted_508(client):
    resp = client.post("/v1/fetch", json={
        "url": f"http://demo.local:{client.demo_port}/large?bytes=100000",
        "max_bytes": 1024,
        "grants": [{"host": "demo.local", "scheme": "http",
                    "port": client.demo_port, "records": ["127.0.0.1"]}],
    })
    assert resp.status_code == 508
    assert resp.json()["failure"]["reason"] == "response.too_large"


def test_fetch_bad_url_is_400(client):
    resp = client.post("/v1/fetch", json={"url": "not-a-url"})
    assert resp.status_code == 400
    assert resp.json()["failure"]["kind"] == "input_error"


def test_runs_listing_get_and_verify(client):
    r1 = client.post("/v1/fetch", json={"url": "http://multi-meta.test/"})
    assert r1.status_code == 403
    run_id = r1.json()["run_id"]

    listing = client.get("/v1/runs").json()["runs"]
    assert any(r["run_id"] == run_id for r in listing)
    only_deny = client.get("/v1/runs?verdict=deny").json()["runs"]
    assert all(r["verdict"] == "deny" for r in only_deny)

    detail = client.get(f"/v1/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run_id"] == run_id

    verify = client.get(f"/v1/runs/{run_id}/verify").json()
    assert verify["valid"] is True
    assert len(verify["digest_sha256"]) == 64

    assert client.get("/v1/runs/no-such-run").status_code == 404


def test_public_key_endpoint(client):
    data = client.get("/v1/key").json()
    assert data["algorithm"] == "Ed25519"
    assert "BEGIN PUBLIC KEY" in data["public_key_pem"]


def test_per_request_grants_do_not_leak_between_runs(client):
    body_allowed = {
        "url": f"http://demo.local:{client.demo_port}/ok",
        "grants": [{"id": "g-once", "host": "demo.local", "scheme": "http",
                    "port": client.demo_port, "records": ["127.0.0.1"]}],
    }
    assert client.post("/v1/fetch", json=body_allowed).status_code == 200
    # 不带 grant 的第二请求：默认 deny（grant 不跨 run 泄漏）
    second = client.post("/v1/fetch",
                         json={"url": f"http://demo.local:{client.demo_port}/ok"})
    assert second.status_code == 403
    assert second.json()["failure"]["reason"] in ("ip.blocked", "policy.default_deny")


def test_rebinding_script_does_not_leap_between_web_runs(client):
    # 每次 /v1/fetch 内部使用 resolver.clone()，重绑定脚本计数应每 run 重置
    for _ in range(2):
        resp = client.post("/v1/fetch",
                           json={"url": "http://rebind-meta-then-public.test/"})
        data = resp.json()
        # 两跳都应在第一帧 169.254.169.254 被拒绝（若计数泄漏，第二 run 会变）
        assert resp.status_code == 403
        assert data["hops"][0]["resolved"]["addresses"][0]["canonical_ip"] == "169.254.169.254"
