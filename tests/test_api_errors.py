"""API 错误语义：四类失败必须可区分；状态隔离与写生命周期。"""
from __future__ import annotations

from app.parser import sha256_hex


def _create_run(client, run_id: str = "r1") -> None:
    r = client.post("/v1/runs", json={"run_id": run_id})
    assert r.status_code == 201, r.text


def _policy() -> dict:
    return {
        "name": "p", "cache_scope": "shared", "vary_headers": ["Accept-Language"],
        "include_authorization": True, "include_cookie": True,
        "allow_storing_authorization_response": True,
        "allow_storing_cookie_response": True,
        "respect_response_vary": True, "vary_wildcard_mode": "forbid",
    }


def _ev(eid: str = "e1", *, body: str = "ok", headers=None, req_headers=None) -> dict:
    return {
        "id": eid, "source": "synthetic_fixture",
        "request": {"method": "get", "scheme": "https", "host": "h",
                    "path": "/a", "query": "",
                    "headers": req_headers or {"Accept-Language": "en"}},
        "response": {"status": 200,
                     "headers": headers or {"Cache-Control": "max-age=10",
                                            "Vary": "Accept-Language"},
                     "body": body, "body_sha256": sha256_hex(body)},
    }


def test_run_not_found_is_404_state_conflict(client):
    r = client.get("/v1/runs/nope/analysis")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["category"] == "state_conflict"
    assert err["code"] == "run_not_found"


def test_duplicate_run_id_conflict(client):
    _create_run(client)
    r = client.post("/v1/runs", json={"run_id": "r1"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "run_exists"


def test_invalid_run_id_is_input_error(client):
    r = client.post("/v1/runs", json={"run_id": "bad id!!"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "run_id_invalid"


def test_policy_before_evidence_required(client):
    _create_run(client)
    r = client.post("/v1/runs/r1/evidence", json={"evidence": [_ev()]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "policy_not_set"


def test_policy_locks_after_evidence(client, log_record):
    _create_run(client)
    client.put("/v1/runs/r1/policy", json=_policy())
    r = client.post("/v1/runs/r1/evidence", json={"evidence": [_ev()]})
    assert r.status_code == 201
    r = client.put("/v1/runs/r1/policy", json=_policy())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "policy_locked"
    log_record("state", run="r1", event="policy_locked_after_evidence")


def test_sealed_run_rejects_writes(client):
    _create_run(client)
    client.put("/v1/runs/r1/policy", json=_policy())
    client.post("/v1/runs/r1/evidence", json={"evidence": [_ev()]})
    assert client.post("/v1/runs/r1/seal").status_code == 200
    r = client.post("/v1/runs/r1/evidence", json={"evidence": [_ev("e2")]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "run_not_open"
    r = client.post("/v1/runs/r1/seal")
    assert r.status_code == 409 and r.json()["error"]["code"] == "run_not_open"


def test_analyze_without_evidence_conflict(client):
    _create_run(client)
    client.put("/v1/runs/r1/policy", json=_policy())
    r = client.post("/v1/runs/r1/analyze")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "nothing_to_analyze"


def test_body_hash_mismatch_is_computation_failure(client):
    _create_run(client)
    client.put("/v1/runs/r1/policy", json=_policy())
    ev = _ev()
    ev["response"]["body_sha256"] = "0" * 64
    r = client.post("/v1/runs/r1/evidence", json={"evidence": [ev]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["category"] == "computation_failure"
    assert err["code"] == "body_hash_mismatch"
    # 失败必须不留半截证据（批次原子性）
    assert client.get("/v1/runs/r1").json()["evidence_count"] == 0


def test_request_validation_error_shape(client):
    # path 不以 / 开头 → pydantic 422
    _create_run(client)
    client.put("/v1/runs/r1/policy", json=_policy())
    ev = _ev()
    ev["request"]["path"] = "noslash"
    r = client.post("/v1/runs/r1/evidence", json={"evidence": [ev]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "request_validation"


def test_policy_wildcard_vary_rejected(client):
    _create_run(client)
    p = _policy()
    p["vary_headers"] = ["*"]
    r = client.put("/v1/runs/r1/policy", json=p)
    assert r.status_code == 422


def test_duplicate_evidence_id_input_error(client):
    _create_run(client)
    client.put("/v1/runs/r1/policy", json=_policy())
    r = client.post("/v1/runs/r1/evidence", json={"evidence": [_ev("dup"), _ev("dup")]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "evidence_invalid"


def test_runs_are_isolated(client):
    """不同 run 即便用同名证据 id 也互不影响，且不存在的 run 不泄漏。"""
    client.post("/v1/runs", json={"run_id": "a"})
    client.post("/v1/runs", json={"run_id": "b"})
    client.put("/v1/runs/a/policy", json=_policy())
    client.put("/v1/runs/b/policy", json=_policy())
    assert client.post("/v1/runs/a/evidence", json={"evidence": [_ev("same-id")]}).status_code == 201
    assert client.post("/v1/runs/b/evidence", json={"evidence": [_ev("same-id")]}).status_code == 201
    # 交叉封口不影响另一个
    client.post("/v1/runs/a/seal")
    assert client.post("/v1/runs/b/evidence",
                       json={"evidence": [_ev("e2", body="different")]}).status_code == 201
    assert client.get("/v1/runs/b").json()["evidence_count"] == 2
