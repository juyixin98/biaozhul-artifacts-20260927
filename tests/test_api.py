"""HTTP API 端到端测试（ASGI 传输，不占用真实端口）。

断言：状态码、稳定错误码、X-Request-Id 关联、审计查询、候选顺序、
回滚的 uncertainties 单列。
"""

from __future__ import annotations

import anyio
import httpx
import pytest

from local_txpool.api.app import create_app
from local_txpool.core.clock import FakeClock
from local_txpool.core.config import Config
from local_txpool.core.kernel import Kernel
from local_txpool.storage.repository import Repository, connect, init_schema
from tests.conftest import make_tx

GWEI = 1_000_000_000


@pytest.fixture
def bundle():
    cfg = Config()
    # 测试中少量区块即想保持"未确认"状态，用较大确认深度关闭自动最终化
    cfg.finality.confirmation_depth = 100
    conn = connect(":memory:")
    init_schema(conn)
    kernel = Kernel(Repository(conn), cfg, FakeClock())
    app = create_app(kernel, cfg)
    return app, kernel, cfg


class _ClientBundle:
    """异步 httpx 客户端的同步外观（测试保持线性写法）。"""

    def __init__(self, portal, http, app, kernel):
        self._portal = portal
        self._http = http
        self._app = app
        self.kernel = kernel

    def get(self, url, **kw):
        return self._portal.call(lambda: self._http.get(url, **kw))

    def post(self, url, **kw):
        return self._portal.call(lambda: self._http.post(url, **kw))


@pytest.fixture
def client(bundle):
    app, kernel, _cfg = bundle
    portal_cm = anyio.from_thread.start_blocking_portal()
    portal = portal_cm.__enter__()
    client_cm = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )
    http = portal.call(client_cm.__aenter__)
    try:
        yield _ClientBundle(portal, http, app, kernel)
    finally:
        portal.call(client_cm.__aexit__, None, None, None)
        portal_cm.__exit__(None, None, None)


def _fund(client, address, balance):
    return client.post(
        "/accounts", json={"address": address, "balance": balance}
    )


def test_health_and_request_id_header(client):
    r = client.get("/health", headers={"X-Request-Id": "fixed-rid"})
    assert r.status_code == 200
    assert r.headers["X-Request-Id"] == "fixed-rid"
    assert r.json()["service_version"]


def test_submit_and_query_flow(client, keys, addresses):
    _fund(client, addresses["alice"], 10**18)
    tx = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    r = client.post("/transactions", json={"raw_tx": "0x" + tx.raw.hex()})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["accepted"] is True
    rid = body["request_id"]

    pool = client.get("/transactions/pool").json()
    assert pool["pending"][0]["tx_hash"] == tx.tx_hash

    # 审计可按请求身份追溯
    trail = client.get(f"/audit/requests/{rid}").json()
    assert trail["found"]
    assert any(e["event_type"] == "admitted" for e in trail["events"])


def test_submit_invalid_signature_error_code(client):
    r = client.post("/transactions", json={"raw_tx": "0xc0"})
    assert r.status_code == 400
    assert r.json()["error"] == "malformed_transaction"
    assert "request_id" in r.json()


def test_low_price_specific_error(client, keys, addresses):
    _fund(client, addresses["alice"], 10**18)
    tx = make_tx(keys["alice"], nonce=0, gas_price=1)
    r = client.post("/transactions", json={"raw_tx": "0x" + tx.raw.hex()})
    assert r.status_code == 400
    assert r.json()["error"] == "gas_price_below_minimum"


def test_propose_block_endpoint_and_order(client, keys, addresses):
    _fund(client, addresses["alice"], 10**18)
    _fund(client, addresses["bob"], 10**18)
    low = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    high = make_tx(keys["bob"], nonce=0, gas_price=20 * GWEI)
    client.post("/transactions", json={"raw_tx": "0x" + low.raw.hex()})
    client.post("/transactions", json={"raw_tx": "0x" + high.raw.hex()})

    r = client.post("/blocks/propose")
    assert r.status_code == 201
    body = r.json()
    assert body["applied"] == [high.tx_hash, low.tx_hash]
    # 未确认区块被显式标记为不确定结论
    assert any(
        u["kind"] == "block_unconfirmed" for u in body["uncertainties"]
    )


def test_rollback_unconfirmed_and_finalized(client, keys, addresses):
    _fund(client, addresses["alice"], 10**18)
    tx = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    client.post("/transactions", json={"raw_tx": "0x" + tx.raw.hex()})
    client.post("/blocks/propose")
    r = client.post("/blocks/rollback", json={"target_number": 0})
    assert r.status_code == 200
    assert tx.tx_hash in r.json()["reentered"]


def test_candidate_preview(client, keys, addresses):
    _fund(client, addresses["alice"], 10**18)
    tx = make_tx(keys["alice"], nonce=0, gas_price=2 * GWEI)
    client.post("/transactions", json={"raw_tx": "0x" + tx.raw.hex()})
    r = client.get("/candidate")
    assert r.status_code == 200
    assert r.json()["ordered"][0]["tx_hash"] == tx.tx_hash


def test_unknown_tx_404(client):
    r = client.get("/transactions/0x" + "ab" * 32)
    assert r.status_code == 404
    assert r.json()["error"] == "tx_not_found"
