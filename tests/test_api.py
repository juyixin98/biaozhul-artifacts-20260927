"""API tests: concrete results and the full error-category mapping."""
from __future__ import annotations

from conftest import BROKEN_POLICY, BROKEN_SHARED_IDENTITY_POLICY, FIXED_POLICY


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["key_id"].startswith("ed25519-")


def test_validate_policy_ok(client):
    r = client.post("/policies/validate", json={"policy": FIXED_POLICY})
    assert r.status_code == 200
    assert r.json()["valid"] is True


def test_validate_policy_input_error(client):
    r = client.post("/policies/validate", json={"policy": {"name": "bad"}})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["category"] == "input"
    assert body["error"]["code"] == "policy.dimensions_missing"


def test_unknown_dimension_is_input_error(client):
    doc = {"name": "p", "covered_dimensions": ["path", "x-telepathy"]}
    r = client.post("/policies/validate", json={"policy": doc})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["category"] == "input"
    assert body["error"]["code"] == "policy.unknown_dimension"
    assert body["error"]["detail"]["unknown"] == ["x-telepathy"]


def test_dangling_response_is_input_error(client):
    body = {
        "policy": BROKEN_POLICY,
        "evidence": {
            "requests": [{"id": "r1", "method": "GET", "path": "/x"}],
            "responses": [{"request_id": "ghost", "status": 200, "body": ""}],
        },
    }
    r = client.post("/audits", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "evidence.dangling_response"


def test_audit_language_counterexample_then_fix(client):
    # Broken key: same key, different language responses -> one collision.
    r1 = client.post("/audits", json={"policy": BROKEN_POLICY, "fixture": "language"})
    assert r1.status_code == 201
    broken = r1.json()
    run_broken = broken["run_id"]
    assert broken["summary"]["collisions"] == 1
    collision = next(f for f in broken["findings"] if f["kind"] == "collision")
    assert {collision["witness"]["request_a"], collision["witness"]["request_b"]} == {
        "r-zh",
        "r-en",
    }
    assert collision["witness"]["differing_dimensions"] == ["accept-language"]

    # Fixed key: collisions gone.
    r2 = client.post("/audits", json={"policy": FIXED_POLICY, "fixture": "language"})
    assert r2.status_code == 201
    fixed = r2.json()
    assert fixed["summary"]["collisions"] == 0
    assert fixed["run_id"] != run_broken


def test_identity_collision_counterexample_shared_policy(client):
    r = client.post(
        "/audits",
        json={"policy": BROKEN_SHARED_IDENTITY_POLICY, "fixture": "identity"},
    )
    assert r.status_code == 201
    report = r.json()
    assert report["summary"]["critical"] >= 3  # identity_shared x2 + collision
    collision = next(f for f in report["findings"] if f["kind"] == "collision")
    assert collision["severity"] == "critical"
    assert collision["witness"]["differing_dimensions"] == ["identity"]


def test_duplicate_run_id_is_state_conflict(client):
    body = {"policy": BROKEN_POLICY, "fixture": "language", "run_id": "run-replay-1"}
    assert client.post("/audits", json=body).status_code == 201
    again = client.post("/audits", json=body)
    assert again.status_code == 409
    assert again.json()["error"]["category"] == "state"
    assert again.json()["error"]["code"] == "run.duplicate"


def test_missing_run_is_404_state(client):
    r = client.get("/audits/run-does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "state"
    assert r.json()["error"]["code"] == "run.not_found"


def test_request_limit_is_resource_exhaustion(client):
    policy = {**BROKEN_POLICY, "limits": {"max_requests": 1, "max_findings": 100}}
    r = client.post("/audits", json={"policy": policy, "fixture": "language"})
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["category"] == "resource"
    assert body["error"]["code"] == "limit.requests"
    assert body["error"]["detail"] == {"count": 2, "limit": 1}


def test_uncanonicalisable_header_is_computation_error(client):
    body = {
        "policy": {
            "name": "p",
            "covered_dimensions": ["path", "accept-language"],
            "identity": {"mode": "auto"},
        },
        "evidence": {
            "requests": [
                {"id": "r1", "method": "GET", "path": "/x",
                 "headers": {"Accept-Language": ["en", "de"]}}
            ],
            "responses": [{"request_id": "r1", "status": 200, "body": "x"}],
        },
    }
    r = client.post("/audits", json=body)
    assert r.status_code == 500
    assert r.json()["error"]["category"] == "computation"
    assert r.json()["error"]["code"] == "compute.header_type"


def test_report_persisted_and_replay_log_available(client):
    r = client.post("/audits", json={"policy": BROKEN_POLICY, "fixture": "language"})
    run_id = r.json()["run_id"]

    fetched = client.get(f"/audits/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == run_id

    events = client.get(f"/audits/{run_id}/events")
    assert events.status_code == 200
    names = [e["event"] for e in events.json()["events"]]
    assert names[:1] == ["run_started"]
    assert "group_formed" in names
    assert names[-1] == "run_finished"

    verify = client.get(f"/audits/{run_id}/verify")
    assert verify.status_code == 200
    assert verify.json()["valid"] is True


def test_no_evidence_is_input_error(client):
    r = client.post("/audits", json={"policy": BROKEN_POLICY})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "evidence.no_source"


def test_bad_run_id_pattern_rejected_by_schema(client):
    body = {"policy": BROKEN_POLICY, "fixture": "language", "run_id": "../escape"}
    r = client.post("/audits", json=body)
    assert r.status_code == 422  # pydantic pattern failure
