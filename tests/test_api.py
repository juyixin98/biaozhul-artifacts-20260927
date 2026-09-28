"""End-to-end HTTP tests against the FastAPI app (in-process transport)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from osdiff.api import create_app
from osdiff.config import Config
from osdiff.service import Service

from . import fixtures as fx


@pytest.fixture()
def client(tmp_path):
    cfg = Config(data_dir=str(tmp_path), db_path=str(tmp_path / "db.sqlite3"),
                 key_path=str(tmp_path / "k.pem"))
    svc = Service(cfg)
    app = create_app(svc)
    with TestClient(app) as c:
        yield c
    svc.close()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_diff_endpoint_reports_conclusion_and_witnesses(client):
    r = client.post("/v1/diff", json={
        "old_policy": fx.EMPTY, "new_policy": fx.PHOTO_V2_EXPAND,
        "client_ref": "ticket-123",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["client_ref"] == "ticket-123"
    assert body["conclusion"]["expands_proven_access"] is True
    assert body["result"]["counts"]["EXPANSION_PROVEN"] >= 1
    witness = body["result"]["witnesses"][0]
    assert witness["old_verdict"] == "DENY_NO_MATCH"
    assert witness["new_trace"]  # real evaluation steps are exposed


def test_diff_parse_error_is_dedicated_failure(client):
    r = client.post("/v1/diff", json={"old_policy": fx.BAD_GLOB, "new_policy": fx.EMPTY})
    assert r.status_code == 400
    body = r.json()
    assert body["failure_code"] == "PARSE_ERROR"
    assert body["details"]["errors"]


def test_space_limit_returns_422_with_attempted_size(tmp_path):
    cfg = Config(data_dir=str(tmp_path), db_path=str(tmp_path / "db.sqlite3"),
                 key_path=str(tmp_path / "k.pem"), space_cap=1)
    svc = Service(cfg)
    with TestClient(create_app(svc)) as c:
        r = c.post("/v1/diff", json={"old_policy": fx.EMPTY, "new_policy": fx.EMPTY})
    svc.close()
    assert r.status_code == 422
    body = r.json()
    assert body["failure_code"] == "SPACE_LIMIT_EXCEEDED"
    assert body["details"]["attempted"] > body["details"]["cap"]


def test_verify_endpoint_unknown_is_separate_from_allow(client):
    r = client.post("/v1/verify", json={
        "policy": fx.NEG_V2,
        "request": {"principal": "alice", "action": "s3:GetObject",
                    "resource": "docs/x", "attributes": {"department": {"__unknown__": True}}},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "UNKNOWN"
    assert body["consistent"] is True


def test_run_persistence_roundtrip_and_filtered_witnesses(client):
    run_id = client.post("/v1/diff", json={"old_policy": fx.EMPTY, "new_policy": fx.PHOTO_V2_EXPAND}).json()["run_id"]
    got = client.get(f"/v1/runs/{run_id}")
    assert got.status_code == 200 and got.json()["status"] == "complete"

    w = client.get(f"/v1/runs/{run_id}/witnesses", params={"category": "EXPANSION_PROVEN"})
    assert w.status_code == 200 and w.json()["witnesses"]
    none = client.get(f"/v1/runs/{run_id}/witnesses", params={"category": "CONTRACTION"})
    assert none.json()["witnesses"] == []

    missing = client.get("/v1/runs/run_does_not_exist")
    assert missing.status_code == 404 and missing.json()["failure_code"] == "NOT_FOUND"


def test_audit_endpoint_and_chain_verification(client):
    r = client.post("/v1/diff", json={"old_policy": fx.EMPTY, "new_policy": fx.PHOTO_V2_EXPAND})
    run_id = r.json()["run_id"]
    events = client.get("/v1/audit", params={"run_id": run_id})
    assert events.status_code == 200 and events.json()["count"] >= 3
    verified = client.post("/v1/audit/verify")
    assert verified.status_code == 200 and verified.json()["ok"] is True
    assert verified.json()["events_verified"] == events.json()["count"]


def test_run_signature_endpoint(client):
    run_id = client.post("/v1/diff", json={"old_policy": fx.EMPTY, "new_policy": fx.PHOTO_V2_EXPAND}).json()["run_id"]
    out = client.post(f"/v1/runs/{run_id}/verify-signature")
    assert out.status_code == 200 and out.json()["valid"] is True
