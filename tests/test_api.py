"""End-to-end HTTP integration tests using FastAPI's in-process test client."""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from sqlguard.app import create_app


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health_reports_ready(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert "orders" in body["tables"]
    assert body["policy_id"] == "shop-readonly-v1"


def test_review_accept_round_trip_and_persistence(client):
    payload = {
        "sql": "SELECT id, email FROM users WHERE id = :id",
        "parameters": {"id": 1},
    }
    r = client.post("/api/v1/audit/reviews", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "accept"
    assert body["findings"] == []
    rid = body["request_id"]
    assert body["stored"]["stored"] is True

    fetched = client.get(f"/api/v1/audit/reviews/{rid}")
    assert fetched.status_code == 200
    assert fetched.json()["verdict"] == "accept"


def test_review_reject_value_as_table(client):
    r = client.post("/api/v1/audit/reviews",
                    json={"sql": "SELECT id FROM ?", "parameters": ["orders"]})
    body = r.json()
    assert body["verdict"] == "reject"
    codes = [f["code"] for f in body["findings"]]
    assert "VALUE_USED_AS_IDENTIFIER" in codes
    assert body["diagnostics"]["decision"] == "reject"


def test_review_dynamic_order_slot_accept(client):
    r = client.post("/api/v1/audit/reviews", json={
        "sql": "SELECT id FROM orders ORDER BY ${sort_col} ${dir_kw}",
        "identifiers": {"sort_col": "created_at", "dir_kw": "DESC"},
    })
    assert r.json()["verdict"] == "accept"


def test_review_reports_inert_comment_placeholder(client):
    r = client.post("/api/v1/audit/reviews", json={
        "sql": "SELECT id FROM users -- :fake ?\nWHERE id = :id",
        "parameters": {"id": 1},
    })
    body = r.json()
    assert body["verdict"] == "accept"
    inert = {o["text"] for o in body["inert_occurrences"]}
    assert {":fake", "?"} <= inert


def test_unanalyzable_input_has_diagnostics_and_offset(client):
    r = client.post("/api/v1/audit/reviews", json={"sql": "SELECT 'oops"})
    body = r.json()
    assert body["verdict"] == "unanalyzable"
    assert body["findings"][0]["code"] == "LEX_ERROR"
    assert body["diagnostics"]["at_offset"] is not None
    assert body["diagnostics"]["parser_state"] == "halted"


def test_request_id_header_is_honored(client):
    r = client.post(
        "/api/v1/audit/reviews",
        json={"sql": "SELECT id FROM orders WHERE id = :id",
              "parameters": {"id": 1}},
        headers={"X-Request-Id": "trace-1234"},
    )
    assert r.json()["request_id"] == "trace-1234"


def test_unknown_request_id_returns_404(client):
    r = client.get("/api/v1/audit/reviews/rev_missing")
    assert r.status_code == 404


def test_recent_list_returns_redacted_rows(client):
    client.post("/api/v1/audit/reviews",
                json={"sql": "SELECT id FROM orders WHERE id = :id",
                      "parameters": {"id": 1}})
    r = client.get("/api/v1/audit/reviews")
    assert r.status_code == 200
    recs = r.json()["records"]
    assert recs and all("verdict" in x for x in recs)


def test_blank_sql_is_422(client):
    r = client.post("/api/v1/audit/reviews", json={"sql": "   "})
    assert r.status_code == 422


def test_sensitive_binding_not_echoed(client):
    r = client.post("/api/v1/audit/reviews", json={
        "sql": "UPDATE users SET email = :api_key WHERE id = :id",
        "parameters": {"api_key": "AKIA-PRIVATE-KEY", "id": 1},
    })
    text = r.text
    assert "AKIA-PRIVATE-KEY" not in text


def test_array_parameter_round_trip(client):
    r = client.post("/api/v1/audit/reviews", json={
        "sql": "SELECT id FROM orders WHERE status = ANY(:s)",
        "parameters": {"s": ["paid", "shipped"]},
    })
    assert r.json()["verdict"] == "accept"
