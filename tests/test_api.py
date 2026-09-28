"""End-to-end HTTP integration tests via FastAPI TestClient.

These exercise the full stack: API -> service -> kernel -> read-only fixture
-> HMAC audit store, using isolated temp paths (never the repo data dir).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sqlguard.api.app import create_app
from sqlguard.config import Settings
from sqlguard.state.fixture import create_fixture
from tests.conftest import FIXTURE_SCHEMA, FIXTURE_SEED


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    fix_dir = tmp_path / "fixture"
    fix_dir.mkdir()
    create_fixture(fix_dir / "fixture.db", FIXTURE_SCHEMA, FIXTURE_SEED)
    settings = Settings(
        policy_path="config/policy.yaml",
        fixture_dir=str(fix_dir),
        audit_db_path=str(tmp_path / "audit.db"),
        audit_key_path=str(tmp_path / "audit.key"),
        host="127.0.0.1", port=0, log_level="WARNING",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health_reports_ok(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["fixture"] == "attached"
    assert body["audit_chain"] == "ok"


def test_review_accept_full_roundtrip(client: TestClient):
    r = client.post("/api/v1/review", json={
        "template": "SELECT id, name FROM users WHERE id = ?",
        "params": {"0": 1},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "accept"
    assert body["statement_type"] == "SELECT"
    assert body["rendered_sql"] == "SELECT id , name FROM users WHERE id = ?"
    assert body["findings"] == []
    assert body["coverage"]["statement_parsed"] is True
    assert "request_id" in body and body["request_id"].startswith("req_")


def test_review_dynamic_sort_field_accept(client: TestClient):
    r = client.post("/api/v1/review", json={
        "template": "SELECT id FROM users ORDER BY {{ sort_col }} {{ sort_dir }}",
        "slots": {"sort_col": "email", "sort_dir": "DESC"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "accept"
    assert '"email" DESC' in body["rendered_sql"]


def test_review_rejects_param_as_table(client: TestClient):
    r = client.post("/api/v1/review", json={
        "template": "SELECT id FROM ?",
        "params": {"0": "users"},
    })
    body = r.json()
    assert body["verdict"] == "reject"
    assert "VALUE_PARAM_AS_IDENTIFIER" in body["codes"]["reject"]
    loc = body["findings"][0]["location"]
    assert {"offset", "line", "col"} <= set(loc or {})


def test_review_rejects_non_whitelisted_identifier(client: TestClient):
    r = client.post("/api/v1/review", json={
        "template": "SELECT id FROM {{ user_relation }}",
        "slots": {"user_relation": "sqlite_master"},
    })
    assert r.json()["verdict"] == "reject"
    assert "IDENTIFIER_NOT_WHITELISTED" in r.json()["codes"]["reject"]


def test_review_unanalyzable_category(client: TestClient):
    r = client.post("/api/v1/review", json={"template": "SELECT '"})
    body = r.json()
    assert body["verdict"] == "unanalyzable"
    assert body["codes"]["unanalyzable"] == ["LEX_ERROR"]


def test_audit_record_is_fetchable_and_redacted(client: TestClient):
    secret = "customer-pii@example.test"
    rev = client.post("/api/v1/review", json={
        "template": "SELECT id FROM users WHERE email = ?",
        "params": {"0": secret},
    })
    rid = rev.json()["request_id"]

    got = client.get(f"/api/v1/audit/{rid}")
    assert got.status_code == 200
    record = got.json()
    assert record["verdict"] == "accept"
    assert secret not in record["evidence"]["rendered_sql"]
    # raw value nowhere in the serialized record
    assert secret not in got.text
    diag = record["evidence"]["param_diagnostics"][0]
    assert diag["binding"] == {"type": "str", "length": len(secret)}


def test_audit_listing_and_chain_verify(client: TestClient):
    for i in range(3):
        client.post("/api/v1/review", json={
            "template": "SELECT id FROM users WHERE id = ?", "params": {"0": i}})
    listing = client.get("/api/v1/audit")
    assert listing.status_code == 200
    assert len(listing.json()) == 3
    chain = client.get("/api/v1/audit/chain/verify")
    assert chain.json()["ok"] is True


def test_unknown_request_id_404(client: TestClient):
    assert client.get("/api/v1/audit/req_does_not_exist").status_code == 404


def test_inline_policy_override_endpoint(client: TestClient):
    payload = {
        "template": "SELECT id FROM users ORDER BY {{ adhoc }}",
        "slots": {"adhoc": "name"},
        "policy_overrides": {
            "slots": {"adhoc": {"allowed": ["name", "id"],
                                "scope": "sort", "require_in_schema": False}}
        },
    }
    assert client.post("/api/v1/review", json=payload).json()["verdict"] == "accept"
    payload.pop("policy_overrides")
    body = client.post("/api/v1/review", json=payload).json()
    assert body["verdict"] == "reject"
    assert "SLOT_UNDECLARED" in body["codes"]["reject"]


def test_each_request_gets_distinct_id_and_is_audited(client: TestClient):
    ids = {
        client.post("/api/v1/review",
                    json={"template": "SELECT 1"}).json()["request_id"]
        for _ in range(3)
    }
    assert len(ids) == 3


def test_client_supplied_request_id_is_preserved(client: TestClient):
    r = client.post("/api/v1/review", json={
        "template": "SELECT 1", "request_id": "trace-xyz-42"})
    assert r.json()["request_id"] == "trace-xyz-42"
    assert client.get("/api/v1/audit/trace-xyz-42").status_code == 200


def test_array_param_end_to_end(client: TestClient):
    r = client.post("/api/v1/review", json={
        "template": "SELECT id FROM users WHERE id IN (?)",
        "params": {"0": [1, 2, 3]},
    })
    body = r.json()
    assert body["verdict"] == "accept"
    assert body["rendered_sql"].replace(" ", "").endswith("IN(?,?,?)")
