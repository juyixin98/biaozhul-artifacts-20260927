"""FastAPI 端到端：真实 HTTP 边界 + 本机源站。

使用真实的 OriginServer（仅环回）和 TestClient，覆盖：
* 200 正常抓取 + 403 策略拒绝 + 400 输入错误 + 409 环 + 429 预算；
* /v1/audit 查询与 /verify；
* 决策链随错误响应返回，含 run_id 可回查。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import ORIGIN_PORT, POLICY_PATH, ZONE_PATH
from safeproxy.kernel import SecurityKernel
from safeproxy.net.connector import PinnedHTTPConnector
from safeproxy.net.resolver import FixtureResolver, parse_zone
from safeproxy.rules.loader import load_policy
from safeproxy.rules.policy import PolicyEngine
from safeproxy.service.api import create_app
from safeproxy.service.origin import OriginServer


@pytest.fixture(scope="module")
def origin():
    with OriginServer(ORIGIN_PORT):
        yield


@pytest.fixture
def client(tmp_path, origin):
    audit_db = str(tmp_path / "e2e.sqlite3")
    bundle = load_policy(str(POLICY_PATH))
    table = parse_zone(str(ZONE_PATH))
    kernel = SecurityKernel(
        PolicyEngine(bundle),
        FixtureResolver(table),
        PinnedHTTPConnector(),
    )
    app = create_app(kernel=kernel, audit_db=audit_db)
    return TestClient(app, raise_server_exceptions=True)


def test_fetch_allowed_200(client):
    r = client.post("/v1/fetch", json={"url": f"http://127.0.0.1:{ORIGIN_PORT}/ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["final_verdict"] == "allow"
    assert body["status_code"] == 200
    assert body["connected_peer"][0] == "127.0.0.1"
    assert body["pinned"] == ["127.0.0.1"]
    assert body["body_sha256"]


def test_fetch_metadata_403_and_chain(client):
    r = client.post("/v1/fetch", json={"url": "http://169.254.169.254/"})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"]["code"] == "E_POLICY_DENY"
    rule = {h["matched"]["rule_id"] for h in detail["hops"] if h.get("matched")}
    assert "deny-metadata-ipv4" in rule
    assert detail["run_id"]


def test_fetch_userinfo_403(client):
    r = client.post("/v1/fetch", json={"url": "http://a@169.254.169.254/"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["reason"] == "userinfo_present"


def test_fetch_bad_scheme_400(client):
    r = client.post("/v1/fetch", json={"url": "gopher://127.0.0.1/"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "E_SCHEME_FORBIDDEN"


def test_fetch_empty_body_422(client):
    r = client.post("/v1/fetch", json={})
    assert r.status_code == 422


def test_redirect_allow_to_forbidden_real(client):
    r = client.post(
        "/v1/fetch",
        json={"url": f"http://127.0.0.1:{ORIGIN_PORT}/redirect-meta"},
    )
    assert r.status_code == 403
    detail = r.json()["detail"]
    hops = detail["hops"]
    assert max(h["hop"] for h in hops) == 2
    # 第一跳真实连过本机
    peers = [h["peer_checked"] for h in hops if h["stage"] == "connect" and h["peer_checked"]]
    flat = [p for grp in peers for p in grp]
    assert "127.0.0.1" in flat
    assert "169.254.169.254" not in flat


def test_redirect_loop_409(client):
    r = client.post("/v1/fetch", json={"url": f"http://127.0.0.1:{ORIGIN_PORT}/loop-a"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "E_REDIRECT_LOOP"


def test_redirect_budget_429(client):
    r = client.post("/v1/fetch", json={"url": f"http://127.0.0.1:{ORIGIN_PORT}/many"})
    assert r.status_code == 429
    assert r.json()["detail"]["error"]["code"] == "E_REDIRECT_BUDGET"


def test_audit_list_get_verify(client):
    # 制造一条 allow 一条 deny
    client.post("/v1/fetch", json={"url": f"http://127.0.0.1:{ORIGIN_PORT}/ok"})
    denied = client.post("/v1/fetch", json={"url": "http://169.254.169.254/"}).json()["detail"]

    listing = client.get("/v1/audit/runs").json()["runs"]
    assert len(listing) >= 2

    deny_runs = client.get("/v1/audit/runs", params={"verdict": "deny"}).json()["runs"]
    assert all(r["verdict"] == "deny" for r in deny_runs)

    one = client.get(f"/v1/audit/runs/{denied['run_id']}").json()
    assert one["verdict"] == "deny"
    assert one["record"]["run_id"] == denied["run_id"]

    assert client.get("/v1/audit/runs/does-not-exist").status_code == 404

    verify = client.get("/v1/audit/verify").json()
    assert verify["ok"] is True and verify["records"] >= 2
