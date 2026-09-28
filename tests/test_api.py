"""End-to-end HTTP tests against the FastAPI application.

These use FastAPI's in-process TestClient (httpx + uvicorn ASGI transport) so
no network or external service is involved. Each test passes its own
``X-Run-Id`` and later pulls audit events by that id, proving the logs are
correlatable to a run and contain versions, steps and judgement bases.
"""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.parsing.fixtures import fixture_payload
from app.security.redaction import assert_no_secret_markers


@pytest.fixture()
def client(tmp_path):
    settings = Settings(  # type: ignore[call-arg]
        db_path=tmp_path / "test.db",
        log_dir=tmp_path / "logs",
        log_level="DEBUG",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, settings


def _create(client, run_id="run-api-1", payload=None):
    c, _ = client
    payload = payload or fixture_payload()
    r = c.post("/api/v1/batches", json=payload, headers={"X-Run-Id": run_id})
    assert r.status_code == 201, r.text
    return r.json()


def test_health_reports_versions(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "1.0.0"
    assert body["protocol_version"] == "audit-commit-v1"


def test_create_batch_public_payload_has_no_salts(client):
    body = _create(client)
    assert body["batch_root_hex"]
    assert len(body["records"]) == 2
    leaks = assert_no_secret_markers(body)
    assert leaks == []
    # Low-entropy boolean must surface an advisory.
    codes = {a["code"] for a in body["advisories"]}
    assert "LOW_ENTROPY_FIELD" in codes


def test_listing_and_get_never_expose_secrets(client):
    c, _ = client
    _create(client)
    for path in ("/api/v1/batches", "/api/v1/batches/batch-synthetic-0001"):
        r = c.get(path)
        assert r.status_code == 200
        assert assert_no_secret_markers(r.json()) == []


def test_disclose_and_verify_happy_path_with_two_verifications(client):
    c, _ = client
    created = _create(client, run_id="run-happy")
    root = created["batch_root_hex"]

    r = c.post(
        "/api/v1/disclose",
        json={
            "batch_id": "batch-synthetic-0001",
            "record_index": 0,
            "path": "subject.age",
        },
        headers={"X-Run-Id": "run-happy"},
    )
    assert r.status_code == 200, r.text
    proof = r.json()["proof"]
    assert proof["claim"]["field_type"] == "int"
    assert proof["reveal"]["value"] == 29
    assert proof["claim"]["commitment_hex"]

    # Verify against caller-supplied trusted root, pinning field identity.
    r = c.post(
        "/api/v1/verify",
        json={
            "proof": proof,
            "trusted_batch_root_hex": root,
            "expected_path": "subject.age",
            "expected_record_index": 0,
        },
        headers={"X-Run-Id": "run-happy"},
    )
    assert r.status_code == 200
    v = r.json()
    assert v["valid"] is True
    assert v["category"] is None
    assert v["claim"]["path"] == "subject.age"
    assert "batch_root_matches_trusted_root" in v["checked_steps"]

    # An independent party holding only root+proof reaches same verdict.
    from independent.verifier import independent_verify

    ok, cat, _, _ = independent_verify(
        proof, root, expected_path="subject.age", expected_record=0
    )
    assert ok and cat is None


def test_disclose_null_and_missing(client):
    c, _ = client
    created = _create(client, run_id="run-null")
    root = created["batch_root_hex"]

    r = c.post(
        "/api/v1/disclose",
        json={"batch_id": created["batch_id"], "record_index": 0,
              "path": "subject.optional_code"},
        headers={"X-Run-Id": "run-null"},
    )
    null_proof = r.json()["proof"]
    assert null_proof["reveal"] == {"value": None, "salt_hex": null_proof["reveal"]["salt_hex"]}

    r = c.post(
        "/api/v1/disclose",
        json={"batch_id": created["batch_id"], "record_index": 1,
              "path": "subject.name_secondary"},
        headers={"X-Run-Id": "run-null"},
    )
    missing_proof = r.json()["proof"]
    assert missing_proof["reveal"] is None

    for proof, state in ((null_proof, "null"), (missing_proof, "missing")):
        r = c.post(
            "/api/v1/verify",
            json={"proof": proof, "trusted_batch_root_hex": root},
            headers={"X-Run-Id": "run-null"},
        )
        assert r.json()["valid"] is True
        assert r.json()["claim"]["state"] == state


def test_wrong_salt_and_wrong_root_return_specific_categories(client):
    c, _ = client
    created = _create(client, run_id="run-bad")
    root = created["batch_root_hex"]
    r = c.post(
        "/api/v1/disclose",
        json={"batch_id": created["batch_id"], "record_index": 0,
              "path": "subject.age"},
        headers={"X-Run-Id": "run-bad"},
    )
    proof = r.json()["proof"]

    # wrong salt -> 200 (verify executed) with valid=false and a category
    bad = copy.deepcopy(proof)
    s = bytes.fromhex(bad["reveal"]["salt_hex"])
    bad["reveal"]["salt_hex"] = (b"\x00" + s[1:]).hex()
    r = c.post(
        "/api/v1/verify",
        json={"proof": bad, "trusted_batch_root_hex": root},
        headers={"X-Run-Id": "run-bad"},
    )
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert r.json()["category"] == "COMMITMENT_MISMATCH"

    # wrong root -> ROOT_MISMATCH
    r = c.post(
        "/api/v1/verify",
        json={"proof": proof, "trusted_batch_root_hex": "ab" * 32},
        headers={"X-Run-Id": "run-bad"},
    )
    v = r.json()
    assert v["valid"] is False
    assert v["category"] == "ROOT_MISMATCH"

    # substituted identity -> IDENTITY_MISMATCH
    other = copy.deepcopy(proof)
    r = c.post(
        "/api/v1/verify",
        json={
            "proof": other,
            "trusted_batch_root_hex": root,
            "expected_path": "subject.score",
        },
        headers={"X-Run-Id": "run-bad"},
    )
    assert r.json()["category"] == "IDENTITY_MISMATCH"


def test_disclose_unknown_batch_and_field_are_404(client):
    c, _ = client
    _create(client)
    r = c.post(
        "/api/v1/disclose",
        json={"batch_id": "nope", "record_index": 0, "path": "subject.age"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BATCH_NOT_FOUND"

    r = c.post(
        "/api/v1/disclose",
        json={"batch_id": "batch-synthetic-0001", "record_index": 0,
              "path": "not.a.field"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "FIELD_NOT_COMMITTED"


def test_bad_payload_is_422_not_success(client):
    c, _ = client
    r = c.post(
        "/api/v1/batches",
        json={"batch_id": "b", "fields": [], "records": []},
    )
    assert r.status_code == 422

    r = c.post(
        "/api/v1/batches",
        json={
            "batch_id": "b2",
            "fields": [{"path": "x", "type": "weird"}],
            "records": [{"x": 1}],
        },
    )
    assert r.status_code == 422


def test_audit_trail_is_run_correlated_and_redacted(client):
    c, settings = client
    created = _create(client, run_id="run-audit")
    root = created["batch_root_hex"]
    r = c.post(
        "/api/v1/disclose",
        json={"batch_id": created["batch_id"], "record_index": 0,
              "path": "subject.age"},
        headers={"X-Run-Id": "run-audit"},
    )
    proof = r.json()["proof"]
    c.post(
        "/api/v1/verify",
        json={"proof": proof, "trusted_batch_root_hex": root},
        headers={"X-Run-Id": "run-audit"},
    )

    r = c.get("/api/v1/audit/events", params={"run_id": "run-audit"})
    events = r.json()["events"]
    types = [e["event_type"] for e in events]
    assert types == ["BATCH_COMMITTED", "FIELD_DISCLOSED", "PROOF_VERIFIED"]
    assert all(e["run_id"] == "run-audit" for e in events)
    assert events[-1]["verdict"] == "ACCEPT"
    assert "checked_steps" in events[-1]["detail"]
    # Audit detail must be free of secret markers.
    assert assert_no_secret_markers(events) == []

    # Service log file contains the run id and version markers.
    log_text = (settings.log_dir / "service.log").read_text()
    assert "run-audit" in log_text
    assert "proto=audit-commit-v1" in log_text
    assert "verdict ACCEPT" in log_text


def test_rejected_proof_is_audited_as_reject_not_ok(client):
    c, _ = client
    created = _create(client, run_id="run-reject")
    r = c.post(
        "/api/v1/disclose",
        json={"batch_id": created["batch_id"], "record_index": 0,
              "path": "subject.is_adult"},
        headers={"X-Run-Id": "run-reject"},
    )
    proof = r.json()["proof"]
    bad = copy.deepcopy(proof)
    bad["reveal"]["value"] = not bad["reveal"]["value"]  # flip boolean
    c.post(
        "/api/v1/verify",
        json={"proof": bad, "trusted_batch_root_hex": created["batch_root_hex"]},
        headers={"X-Run-Id": "run-reject"},
    )
    events = c.get(
        "/api/v1/audit/events", params={"run_id": "run-reject"}
    ).json()["events"]
    last = events[-1]
    assert last["verdict"] == "REJECT"
    assert last["category"] == "COMMITMENT_MISMATCH"
