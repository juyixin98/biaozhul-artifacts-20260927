"""HTTP API: request correlation, explainability, separated failure/uncertainty."""

import pytest
from fastapi.testclient import TestClient

from basefee_model.api.main import app
from basefee_model.fixtures import canonical_scenario

client = TestClient(app)


def test_health_and_version_carry_identity():
    rid = "req-test-123"
    resp = client.get("/version", headers={"X-Request-Id": rid})
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == rid
    body = resp.json()
    assert body["request_id"] == rid
    assert body["version"]
    assert body["result"]["params"]["elasticity_multiplier"] == 2
    assert "integer_semantics" in body["result"]


def test_next_base_fee_explainable():
    body_in = {"parent_base_fee": 1_000_000_000,
               "parent_gas_used": 0, "parent_gas_limit": 30_000_000}
    resp = client.post("/basefee/next", json=body_in)
    assert resp.status_code == 200
    out = resp.json()
    assert out["result"]["next_base_fee"] == 875_000_000
    assert out["result"]["direction"] == "down"
    assert out["steps"][0]["host"].startswith("basefee_model.core.fees")
    assert out["failures"] == [] and out["uncertainties"] == []
    assert out["request_id"]  # generated when not supplied


def test_validate_fee_type2_ok():
    resp = client.post("/transactions/validate-fee", json={
        "tx_type": 2, "base_fee": 100, "gas_limit": 21_000,
        "max_fee_per_gas": 200, "max_priority_fee_per_gas": 50})
    assert resp.status_code == 200
    res = resp.json()["result"]
    assert res["effective_gas_price"] == 150
    assert res["priority_fee_per_gas"] == 50
    assert resp.json()["uncertainties"] == []


def test_validate_fee_legacy_lists_uncertainty_separately():
    resp = client.post("/transactions/validate-fee", json={
        "tx_type": 0, "base_fee": 100, "gas_limit": 21_000, "gas_price": 300})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["effective_gas_price"] == 300
    # Legacy caveat is an uncertainty, NOT a failure.
    assert body["failures"] == []
    assert len(body["uncertainties"]) == 1
    assert body["uncertainties"][0]["kind"] == "legacy_fee_semantics"


def test_validate_fee_invalid_cap_is_failure():
    resp = client.post("/transactions/validate-fee", json={
        "tx_type": 2, "base_fee": 100, "gas_limit": 21_000,
        "max_fee_per_gas": 50, "max_priority_fee_per_gas": 80})
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "error"
    assert body["failures"][0]["code"] == "fee_cap_less_than_priority"
    assert body["result"] is None
    assert body["request_id"] == resp.headers["X-Request-Id"]


def test_replay_endpoint_success_and_conservation():
    sc = canonical_scenario()
    resp = client.post("/replay", json={
        "genesis_base_fee": sc["genesis_base_fee"],
        "gas_limit": sc["gas_limit"], "alloc": sc["alloc"],
        "blocks": sc["blocks"],
    })
    assert resp.status_code == 200, body.text
    body = resp.json()
    assert body["result"]["conservation"]["conserved"] is True
    assert body["result"]["applied"] == [1, 2, 3, 4, 5, 6, 7]
    assert body["steps"][0]["host"].startswith("basefee_model.replay")


def test_replay_endpoint_overfull_block_fails_categorized():
    # One block declaring gas_used over the limit.
    sc = canonical_scenario()
    sc["blocks"] = sc["blocks"][:1]
    sc["blocks"][0]["tx_gas_used"] = [30_000_001]
    resp = client.post("/replay", json={
        "genesis_base_fee": sc["genesis_base_fee"],
        "gas_limit": sc["gas_limit"], "alloc": sc["alloc"],
        "blocks": sc["blocks"],
    })
    assert resp.status_code == 422
    code = resp.json()["failures"][0]["code"]
    assert code == "block_gas_over_limit"
