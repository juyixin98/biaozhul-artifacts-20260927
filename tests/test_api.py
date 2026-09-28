"""HTTP-layer tests using a real ASGI client (httpx via Starlette)."""
from __future__ import annotations

import json

import pytest

from fastapi.testclient import TestClient

from localffg.api import create_app
from localffg.config import AppConfig
from localffg.epochs import ValidatorRegistry
from localffg.fixtures_builder import build_fixture_set


@pytest.fixture()
def client(tmp_path, manifest):
    fixture_paths = build_fixture_set(tmp_path / "fx")
    cfg = AppConfig(db_path=str(tmp_path / "http.db"), allow_bootstrap_api=True)
    app = create_app(cfg)
    svc = app.state.service
    svc.install_registry(ValidatorRegistry.from_json(manifest["registry"]))
    with TestClient(app) as c:
        yield c, svc


def test_health_reports_versions(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "up"
    assert body["app_version"] and body["protocol_version"]
    assert body["chain_id"] == "local-chain-0"


def test_submit_double_vote_end_to_end_and_recheck(client, manifest):
    c, _ = client
    steps = manifest["scenarios"]["S2_double_vote_same_target"]
    r1 = c.post("/v1/votes", json={**steps[0]["signed"], "run_id": "http-run-1"})
    assert r1.status_code == 200 and r1.json()["category"] == "accepted"

    r2 = c.post("/v1/votes", json=steps[1]["signed"])
    assert r2.status_code == 200  # valid envelope, offense reported, not a 4xx
    body = r2.json()
    assert body["category"] == "double_vote" and body["slashable"] is True
    evidence_id = body["evidence"][0]["evidence_id"]

    # independent re-check through the API
    rr = c.post(f"/v1/evidence/{evidence_id}/recheck")
    assert rr.status_code == 200
    check = rr.json()
    assert check["verdict"] == "valid"
    assert check["derived_kind"] == "double_vote"
    assert check["signature_a_ok"] and check["signature_b_ok"]
    assert check["derived_weight"] == 100


def test_invalid_submissions_return_exact_422_categories(client, manifest):
    c, _ = client

    # tampered signature
    env = manifest["scenarios"]["S5_bad_signature"][0]["signed"]
    r = c.post("/v1/votes", json=env)
    assert r.status_code == 422
    assert r.json()["category"] == "invalid_signature"

    # unknown validator
    r = c.post("/v1/votes", json=manifest["scenarios"]["S6_unknown_validator"][0]["signed"])
    assert r.status_code == 422 and r.json()["category"] == "unknown_validator"

    # wrong chain
    r = c.post("/v1/votes", json=manifest["scenarios"]["S7_wrong_chain"][0]["signed"])
    assert r.status_code == 422 and r.json()["category"] == "invalid_chain"

    # bad rounds
    r = c.post("/v1/votes", json=manifest["scenarios"]["S8_bad_rounds"][0]["signed"])
    assert r.status_code == 422 and r.json()["category"] == "invalid_rounds"


def test_malformed_envelope_is_422_malformed(client):
    c, _ = client
    r = c.post("/v1/votes", json={"chain_id": "x", "validator_id": "alice",
                                  "source_round": 0, "target_round": 1,
                                  "block_root": "not-hex!!",
                                  "signer_pubkey": "aa", "signature": "bb"})
    assert r.status_code == 422
    assert r.json()["detail"]["category"] == "malformed"


def test_duplicate_retransmit_is_200_not_slashable(client, manifest):
    c, _ = client
    for step in manifest["scenarios"]["S1_duplicate_retransmit"]:
        r = c.post("/v1/votes", json=step["signed"])
        assert r.status_code == 200
    last = r.json()
    assert last["category"] == "duplicate_retransmit"
    assert last["slashable"] is False and last["evidence"] == []


def test_stats_and_evidence_listing_and_replay(client, manifest):
    c, _ = client
    for name in ["S3_surround_nested", "S4_membership_change"]:
        for step in manifest["scenarios"][name]:
            c.post("/v1/votes", json=step["signed"])

    stats = c.get("/v1/votes/stats").json()
    assert stats["journal"]["surround_vote"] == 1
    assert stats["journal"]["invalid_membership"] == 2

    listing = c.get("/v1/evidence").json()
    assert listing["count"] == 1

    replay = c.post("/v1/replay").json()
    assert replay["verdict"] == "OK", replay["errors"]
    assert replay["evidence_set_match"] is True


def test_bootstrap_and_weight_change_endpoints(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "b.db"), allow_bootstrap_api=True)
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.post("/v1/validators/bootstrap", json={"validator_id": "val-x", "weight": 10})
        assert r.status_code == 200
        assert len(r.json()["signer_pubkey"]) == 64
        # duplicate bootstrap is a conflict, not a silent success
        r2 = c.post("/v1/validators/bootstrap", json={"validator_id": "val-x", "weight": 10})
        assert r2.status_code == 409
        # weight update and rejection of unknown validator
        assert c.post("/v1/validators/val-x/weight",
                      json={"effective_epoch": 3, "weight": 25}).status_code == 200
        assert c.post("/v1/validators/nobody/weight",
                      json={"effective_epoch": 1, "weight": 1}).status_code == 404


def test_bootstrap_disabled_returns_403(tmp_path):
    cfg = AppConfig(db_path=str(tmp_path / "b2.db"), allow_bootstrap_api=False)
    app = create_app(cfg)
    with TestClient(app) as c:
        r = c.post("/v1/validators/bootstrap", json={"validator_id": "z", "weight": 1})
        assert r.status_code == 403
