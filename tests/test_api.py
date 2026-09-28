"""HTTP 适配层测试：用 FastAPI TestClient 覆盖接受/拒绝/查询/重放接口。"""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from teachchain import fixtures
from teachchain.api import create_app


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "api.db")
    app = create_app(db_path=db, seed_fixtures=False)
    with TestClient(app) as c:
        yield c
    app.state.store.close()


@pytest.fixture
def funded(client, alice):
    # 通过内部服务给 alice 注资（合成）
    svc = client.app.state.service
    svc.seed(alice.address, 10_000_000)
    return svc


def _deploy(alice, code=fixtures.counter_code(), nonce=0, gas=500_000):
    return fixtures.envelope(alice, "deploy", nonce=nonce, gas_limit=gas,
                             code=code)


def test_health_reports_engine_version(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["engine_version"].startswith("teachchain-v")
    assert "x-request-id" in {k.lower() for k in r.headers}


def test_submit_deploy_and_invoke_roundtrip(client, funded, alice):
    r = client.post("/tx", json=_deploy(alice))
    assert r.status_code == 200, r.text
    dep = r.json()
    assert dep["status"] == 1 and dep["deployed_address"]
    addr = dep["deployed_address"]

    env = fixtures.envelope(alice, "invoke", nonce=1, gas_limit=500_000,
                            to=addr, words=[3])
    r2 = client.post("/tx", json=env)
    assert r2.status_code == 200, r2.text
    inv = r2.json()
    # counter 合约固定 RETURN mem[0]=1；累加值在存储槽 1
    assert inv["status"] == 1 and inv["output"] == [1]

    # 存储与账户视图
    assert client.get(f"/storage/{addr}/1").json()["value"] == 3
    acct = client.get(f"/accounts/{alice.address}").json()
    assert acct["nonce"] == 2 and acct["balance"] < 10_000_000


def test_rejected_tx_returns_422_and_changes_nothing(client, funded, alice):
    # 非法字节码 -> 422 invalid_bytecode
    bad_env = fixtures.envelope(alice, "deploy", nonce=0, gas_limit=500_000,
                                code=b"\x0f")
    r = client.post("/tx", json=bad_env)
    assert r.status_code == 422
    assert r.json()["code"] == "invalid_bytecode"
    assert r.json()["request_id"]
    # 高度仍为 0
    assert client.get("/health").json()["height"] == 0


def test_bad_nonce_is_rejected(client, funded, alice):
    client.post("/tx", json=_deploy(alice))
    env = fixtures.envelope(alice, "deploy", nonce=0, gas_limit=500_000,
                            code=fixtures.counter_code())
    r = client.post("/tx", json=env)
    assert r.status_code == 422 and r.json()["code"] == "bad_nonce"


def test_receipt_lookup_by_height_and_tx(client, funded, alice):
    dep = client.post("/tx", json=_deploy(alice)).json()
    h = dep["height"]
    by_h = client.get(f"/receipts/{h}")
    assert by_h.status_code == 200 and by_h.json()["tx_hash"] == dep["tx_hash"]
    by_tx = client.get(f"/tx/{dep['tx_hash']}")
    assert by_tx.status_code == 200
    assert client.get("/receipts/9999").status_code == 404


def test_storage_404_when_unset(client, funded, alice):
    client.post("/tx", json=_deploy(alice))
    dep = client.get("/receipts/1").json()
    r = client.get(f"/storage/{dep['deployed_address']}/123")
    assert r.status_code == 404


def test_admin_replay_ok_after_chain(client, funded, alice):
    client.post("/tx", json=_deploy(alice))
    env = fixtures.envelope(alice, "invoke", nonce=1, gas_limit=500_000,
                            to=client.get("/receipts/1").json()["deployed_address"],
                            words=[1])
    client.post("/tx", json=env)
    r = client.post("/admin/replay")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["tx_total"] == 2 and body["matched"] == 2


def test_request_id_echoed_from_header(client):
    r = client.get("/health", headers={"X-Request-ID": "fixed-rid-123"})
    assert r.headers["x-request-id"] == "fixed-rid-123"
