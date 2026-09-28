"""Service (HTTP boundary) tests: status codes, error envelope, categories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# FastAPI's TestClient (uses httpx internally).
from fastapi.testclient import TestClient

from lightclient import codec
from lightclient.config import LightClientConfig
from lightclient.errors import ErrorCategory, ErrorCode
from lightclient.fixtures.builder import ChainBuilder
from lightclient.service import create_app

GOLDEN = json.loads((Path(__file__).parent / "golden_vectors.json").read_text())


@pytest.fixture
def client(tmp_path):
    builder = ChainBuilder()
    _g, env = builder.genesis()
    app = create_app(
        store_path=str(tmp_path / "svc.db"),
        config=LightClientConfig(),
        trusted_checkpoint_key=builder.checkpoint_pub,
        run_id="svc-test",
    )
    with TestClient(app) as c:
        # bootstrap over the wire
        resp = c.post(
            "/bootstrap",
            json={"checkpoint_envelope": codec.encode_envelope(env).hex()},
        )
        assert resp.status_code == 200
        c.builder = builder
        yield c


def test_health_reports_initialized_and_tip(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["initialized"] is True
    assert body["protocol"] == "local-header-lightclient/v1"
    assert body["tip"]["height"] == 0


def test_submit_valid_header_200(client):
    blk = client.builder.add_block(signer_labels=["c0-a", "c0-b"])
    resp = client.post(
        "/headers",
        json={
            "header": codec.encode_header(blk.header).hex(),
            "certificate": codec.encode_certificate(blk.certificate).hex(),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["result"]["decision"] == "accepted"
    assert body["result"]["tip"]["height"] == 1


def test_weight_below_quorum_maps_to_409_state(client):
    before = client.get("/tip").json()["tip"]
    blk = client.builder.add_block(signer_labels=["c0-a"])
    resp = client.post(
        "/headers",
        json={
            "header": codec.encode_header(blk.header).hex(),
            "certificate": codec.encode_certificate(blk.certificate).hex(),
        },
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == ErrorCode.WEIGHT_BELOW_QUORUM.value
    assert err["category"] == ErrorCategory.STATE.value
    assert err["detail"]["signed_weight"] == 1
    # state unchanged after the 409
    after = client.get("/tip").json()["tip"]
    assert after == before


def test_malformed_hex_maps_to_400_input(client):
    resp = client.post("/headers", json={"header": "zz", "certificate": "aa"})
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == ErrorCategory.INPUT.value


def test_non_json_body_maps_to_400(client):
    resp = client.post("/headers", content="not json",
                       headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == ErrorCode.INPUT_MALFORMED.value


def test_need_checkpoint_maps_to_409_and_includes_gap(client):
    ts = GOLDEN["genesis_timestamp"] + GOLDEN["trust_period_seconds"] + 1
    blk = client.builder.add_block(
        signer_labels=["c0-a", "c0-b"], timestamp=ts
    )
    resp = client.post(
        "/headers",
        json={
            "header": codec.encode_header(blk.header).hex(),
            "certificate": codec.encode_certificate(blk.certificate).hex(),
        },
    )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == ErrorCode.NEED_CHECKPOINT.value
    assert err["detail"]["gap_seconds"] == 3601


def test_audit_log_records_accept_and_reject_with_run_id(client):
    blk = client.builder.add_block(signer_labels=["c0-a", "c0-b"])
    client.post(
        "/headers",
        json={
            "header": codec.encode_header(blk.header).hex(),
            "certificate": codec.encode_certificate(blk.certificate).hex(),
        },
    )
    bad = client.builder.add_block(signer_labels=["c0-a"])
    client.post(
        "/headers",
        json={
            "header": codec.encode_header(bad.header).hex(),
            "certificate": codec.encode_certificate(bad.certificate).hex(),
        },
    )
    resp = client.get("/audit?limit=10")
    entries = resp.json()["entries"]
    results = {e["result"] for e in entries}
    assert "accepted" in results
    assert "rejected" in results
    rejected = next(e for e in entries if e["result"] == "rejected")
    assert rejected["error_code"] == ErrorCode.WEIGHT_BELOW_QUORUM.value
    assert rejected["run_id"] == "svc-test"
    # the rejection entry preserves intermediate state for replay
    assert rejected["detail"]["tip_before"] == rejected["detail"]["tip_after"]
    assert rejected["detail"]["state_unchanged"] is True


def test_get_header_indexed_and_unknown_409(client):
    blk = client.builder.add_block(signer_labels=["c0-a", "c0-b"])
    client.post(
        "/headers",
        json={
            "header": codec.encode_header(blk.header).hex(),
            "certificate": codec.encode_certificate(blk.certificate).hex(),
        },
    )
    digest = codec.header_digest(blk.header).hex()
    ok = client.get(f"/headers/{digest}")
    assert ok.status_code == 200
    missing = client.get("/headers/" + "ab" * 32)
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == ErrorCode.PARENT_UNKNOWN.value


def test_oversized_request_body_maps_to_413(client):
    # middleware enforces the configured request bound before JSON parsing
    limit = LightClientConfig().max_request_bytes
    padding = "aa" * (limit // 2 + 10)
    resp = client.post(
        "/headers",
        content=("{" + f'"padding":"{padding}"' + "}"),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["category"] == ErrorCategory.RESOURCE.value


def test_replay_endpoint_reports_failure_index(client):
    good = client.builder.add_block(signer_labels=["c0-a", "c0-b"])
    bad = client.builder.add_block(signer_labels=["c0-a"])
    resp = client.post(
        "/replay",
        json={
            "items": [
                {
                    "header": codec.encode_header(good.header).hex(),
                    "certificate": codec.encode_certificate(good.certificate).hex(),
                },
                {
                    "header": codec.encode_header(bad.header).hex(),
                    "certificate": codec.encode_certificate(bad.certificate).hex(),
                },
            ]
        },
    )
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["status"] == "stopped"
    assert report["applied"] == 1
    assert report["failure_index"] == 1
    assert report["steps"][1]["error_code"] == "WEIGHT_BELOW_QUORUM"
