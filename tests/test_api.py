"""HTTP 接口端到端测试（FastAPI TestClient）。

断言具体错误码信封、X-Request-ID 关联、状态/余额结果——
不只是“接口能调用”。
"""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from localtxpool.api.app import create_app
from localtxpool.encoding import sign_transaction
from tests.conftest import CHAIN_ID, addr_for, key_for


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


def _raw(label, **kw):
    kw.setdefault("nonce", 0); kw.setdefault("gas_price", 10)
    kw.setdefault("gas_limit", 21000); kw.setdefault("to", addr_for("bob"))
    kw.setdefault("value", 0); kw.setdefault("chain_id", CHAIN_ID)
    return "0x" + sign_transaction(key_for(label), **kw).to_rlp().hex()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["chain_id"] == CHAIN_ID
    assert r.headers["X-Service-Version"]


def test_request_id_echo_and_propagation(client):
    r = client.get("/health", headers={"X-Request-ID": "fixed-req-123"})
    assert r.headers["X-Request-ID"] == "fixed-req-123"
    assert r.json()["request_id"] == "fixed-req-123"
    # 未提供时自动生成
    r2 = client.get("/health")
    assert len(r2.headers["X-Request-ID"]) == 32


def test_full_lifecycle_over_http(client, service):
    # 注资
    r = client.post("/admin/fund", json={"address": "0x" + addr_for("alice").hex(),
                                         "amount": str(10**18)})
    assert r.status_code == 200
    # 提交交易
    r = client.post("/transactions", json={"raw": _raw("alice")})
    assert r.status_code == 202
    tx_hash = r.json()["data"]["tx_hash"]
    assert r.json()["data"]["status"] == "pending"
    # 查询
    r = client.get(f"/transactions/{tx_hash}")
    assert r.json()["data"]["status"] == "pending"
    # 候选
    r = client.get("/pool/candidate")
    assert [x["tx_hash"] for x in r.json()["data"]["order"]] == [tx_hash]
    # 提议 -> 确认
    assert client.post("/blocks/propose").status_code == 201
    r = client.post("/blocks/confirm")
    assert r.status_code == 200
    # 链尖
    assert client.get("/blocks/head").json()["data"]["number"] == 1
    # journal 可按请求 id 关联
    # （提议请求的 id 自动生成，改用 tx_hash 维度查询）
    r = client.get("/explain/journals", params={"tx_hash": tx_hash})
    actions = {j["action"] for j in r.json()["data"]}
    assert {"receive", "classify", "include", "mine"} & actions


def test_error_envelope_has_stable_code(client, service):
    # 无余额且不存在的账户提交 -> INSUFFICIENT 前先因... 实际 alice 有合法签名，
    # 未注资 => 分类 queued(INSUFFICIENT_FUNDS) 但提交仍 202；改用坏链 ID 触发 400
    bad = "0x" + sign_transaction(
        key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
        to=addr_for("bob"), value=0, chain_id=999).to_rlp().hex()
    client.post("/admin/fund", json={"address": "0x" + addr_for("alice").hex(),
                                     "amount": str(10**18)})
    r = client.post("/transactions", json={"raw": bad})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "WRONG_CHAIN_ID"
    assert body["request_id"]


def test_validation_error_envelope(client):
    # 缺少必填字段 raw -> pydantic 422
    r = client.post("/transactions", json={"nope": 1})
    assert r.status_code == 422
    assert r.json()["error"] == "BAD_REQUEST"


def test_hex_decode_error_is_400_bad_request(client):
    # 类型正确但内容不是合法 0x hex -> 解码层 BAD_REQUEST
    r = client.post("/transactions", json={"raw": "not-hex"})
    assert r.status_code == 400
    assert r.json()["error"] == "BAD_REQUEST"


def test_unknown_tx_404(client):
    r = client.get("/transactions/0x" + "ab" * 32)
    assert r.status_code == 404
    assert r.json()["error"] == "NOT_FOUND"


def test_replacement_underpriced_over_http(client, service):
    client.post("/admin/fund", json={"address": "0x" + addr_for("alice").hex(),
                                     "amount": str(10**18)})
    client.post("/transactions", json={"raw": _raw("alice", value=1, gas_price=100)})
    r = client.post("/transactions", json={"raw": _raw("alice", value=2, gas_price=101)})
    assert r.status_code == 409
    assert r.json()["error"] == "REPLACEMENT_UNDERPRICED"
    assert r.json()["details"]["required_gas_price"] == 110


def test_rollback_endpoint(client, service):
    client.post("/admin/fund", json={"address": "0x" + addr_for("alice").hex(),
                                     "amount": str(10**18)})
    client.post("/transactions", json={"raw": _raw("alice", value=1)})
    client.post("/blocks/propose")
    client.post("/blocks/confirm")
    r = client.post("/blocks/rollback", json={"n": 1})
    assert r.status_code == 200
    assert r.json()["data"]["reverted_blocks"] == [1]
    # 深度过大
    r = client.post("/blocks/rollback", json={"n": 5})
    assert r.status_code == 400
    assert r.json()["error"] == "ROLLBACK_TOO_DEEP"
