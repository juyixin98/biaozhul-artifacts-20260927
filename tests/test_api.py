"""HTTP 接口：状态码、请求标识、脱敏诊断与端到端封块/查询。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from teaching_chain.api import create_app
from teaching_chain.diagnostics import redact

from .conftest import asm, make_tx


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "api_test.db")
    with TestClient(app) as c:
        yield c
    app.state.node.close()


def test_health_and_status(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "request_id" in body
    assert body["node"]["height"] == -1
    assert "X-Request-ID" in r.headers


def test_client_supplied_request_id_is_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "fixed-id-123"})
    assert r.headers["X-Request-ID"] == "fixed-id-123"
    assert r.json()["request_id"] == "fixed-id-123"


def test_submit_block_success_and_get_receipt(client, alice):
    tx = make_tx(alice, asm("PUSH8 42\nPUSH8 1\nSSTORE\nSTOP"), nonce=1)
    r = client.post("/v1/blocks", json={"transactions": [tx]})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["block_number"] == 0
    receipt = body["receipts"][0]
    assert receipt["status"] == 1
    tx_hash = receipt["tx_hash"]

    r2 = client.get(f"/v1/receipts/{tx_hash}")
    assert r2.status_code == 200
    assert r2.json()["receipt"]["tx_hash"] == tx_hash

    r3 = client.get("/v1/blocks/0")
    assert r3.status_code == 200
    assert r3.json()["block"]["tx_count"] == 1


def test_submit_bad_signature_returns_422_without_block(client, alice):
    tx = make_tx(alice, asm("STOP"), nonce=2)
    tx["signature"] = "11" * 64
    r = client.post("/v1/blocks", json={"transactions": [tx]})
    assert r.status_code == 422
    body = r.json()
    assert body["error_code"] == "TX_BAD_SIGNATURE"
    assert body["request_id"]
    # 未封块
    assert client.get("/v1/status").json()["node"]["height"] == -1


def test_submit_wrong_chain_422(client, alice):
    tx = make_tx(alice, asm("STOP"), chain="nope", nonce=3)
    r = client.post("/v1/blocks", json={"transactions": [tx]})
    assert r.status_code == 422
    assert r.json()["error_code"] == "TX_CHAIN_MISMATCH"


def test_empty_batch_rejected_by_schema(client):
    r = client.post("/v1/blocks", json={"transactions": []})
    assert r.status_code == 422


def test_malformed_json_422(client):
    r = client.post("/v1/blocks", content=b"{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_dry_run_executes_without_committing(client, alice):
    tx = make_tx(alice, asm("PUSH8 7\nPUSH8 1\nSSTORE\nSTOP"), nonce=9)
    r = client.post("/v1/dry-run", json={"transaction": tx})
    assert r.status_code == 200
    assert r.json()["receipt"]["status"] == 1
    assert client.get("/v1/status").json()["node"]["height"] == -1


def test_dry_run_failed_tx_reports_category(client, alice):
    tx = make_tx(alice, asm("PUSH8 1\nPUSH8 0\nDIV\nSTOP"), nonce=10)
    r = client.post("/v1/dry-run", json={"transaction": tx})
    assert r.status_code == 200
    receipt = r.json()["receipt"]
    assert receipt["status"] == 0
    assert receipt["error_category"] == "DIV_BY_ZERO"
    assert receipt["gas_used"] == receipt["gas_limit"]


def test_dry_run_bad_signature_422(client, alice):
    tx = make_tx(alice, asm("STOP"), nonce=11)
    tx["signer"] = "ab" * 20
    r = client.post("/v1/dry-run", json={"transaction": tx})
    assert r.status_code == 422
    assert r.json()["error_code"] == "TX_BAD_SIGNATURE"


def test_execution_failure_in_block_is_persisted(client, alice):
    good = make_tx(alice, asm("PUSH8 5\nPUSH8 1\nSSTORE\nSTOP"), nonce=12)
    bad = make_tx(alice, asm("PUSH8 1\nPUSH8 0\nDIV\nSTOP"), nonce=13)
    r = client.post("/v1/blocks", json={"transactions": [good, bad]})
    assert r.status_code == 200
    statuses = [x["status"] for x in r.json()["receipts"]]
    assert statuses == [1, 0]
    state_root = client.get("/v1/status").json()["node"]["state_root"]
    # 失败交易回滚：状态根应只反映 storage[1]=5
    from teaching_chain.kernel import state_root as compute_root
    assert state_root == compute_root({1: 5})


def test_get_missing_block_and_receipt_404(client):
    assert client.get("/v1/blocks/99").status_code == 404
    assert client.get("/v1/receipts/" + "ab" * 32).status_code == 404


def test_redact_masks_sensitive_fields():
    out = redact("signature", "a" * 128)
    assert "a" * 128 not in out
    assert "128" in out
    assert redact("code", "0x" + "ab" * 20).startswith("<hex 20")
    nested = redact("wrapper", {"pubkey": "ff" * 32, "ok": True})
    assert "ff" * 32 not in json.dumps(nested, ensure_ascii=False)
    assert nested["ok"] is True


def test_persistence_across_node_restart(tmp_path, alice):
    db = tmp_path / "persist.db"
    app1 = create_app(db)
    with TestClient(app1) as c1:
        tx = make_tx(alice, asm("PUSH8 3\nPUSH8 1\nSSTORE\nSTOP"), nonce=20)
        assert c1.post("/v1/blocks", json={"transactions": [tx]}).status_code == 200
    app1.state.node.close()

    app2 = create_app(db)  # 冷启动从索引重建内存状态
    with TestClient(app2) as c2:
        body = c2.get("/v1/status").json()["node"]
        assert body["height"] == 0
        from teaching_chain.kernel import state_root
        assert body["state_root"] == state_root({1: 3})
    app2.state.node.close()
