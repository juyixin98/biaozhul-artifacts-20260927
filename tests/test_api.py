"""HTTP tests: status codes, failure classes, request identity propagation."""
from __future__ import annotations


def test_health_reports_variant(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "adjacent-transposition" in body["variant"]
    assert "unrestricted Damerau" in body["variant"]
    assert body["active_version_id"] is not None
    assert "X-Request-ID" in r.headers


def test_correct_endpoint_full_payload(client):
    r = client.post("/correct", json={"query": "teh", "threshold": 1.0})
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == r.headers["X-Request-ID"]
    assert "unrestricted Damerau" in body["algorithm"]["variant"]
    token = body["tokens"][0]
    assert token["candidates"][0]["candidate"] == "the"
    assert token["candidates"][0]["distance"] == 1.0
    assert token["candidates"][0]["path_replay_verified"] is True
    assert token["stage"]["within_threshold"] >= 1
    assert body["dictionary"]["is_active_version"] is True


def test_request_id_from_header_is_used(client):
    r = client.post("/correct", json={"query": "hello"},
                    headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["X-Request-ID"] == "trace-abc-123"
    assert r.json()["request_id"] == "trace-abc-123"


def test_empty_query_failure_class(client):
    r = client.post("/correct", json={"query": "   "})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "empty_query"
    assert body["request_id"]


def test_normalized_accent_is_accepted_but_symbol_is_rejected(client):
    # é -> e + combining mark -> "cafe", which IS supported after normalization.
    ok = client.post("/correct", json={"query": "café", "threshold": 2.0})
    assert ok.status_code == 200
    assert ok.json()["normalized_query"] == "cafe"
    # § does not decompose into alphabet characters -> rejected.
    bad = client.post("/correct", json={"query": "cafe§"})
    assert bad.status_code == 422
    body = bad.json()
    assert body["error"]["code"] == "unsupported_character"
    assert body["error"]["details"]["position"] == 4


def test_query_too_long_failure_class(client):
    r = client.post("/correct", json={"query": "x" * 65})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "query_too_long"


def test_too_many_tokens_failure_class(client):
    r = client.post("/correct", json={"query": "a b c d e f g h i"})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_many_tokens"


def test_version_not_found_failure_class(client):
    r = client.post("/correct", json={"query": "hello", "version_id": 424242})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_not_found"


def test_invalid_max_results_failure_class(client):
    r = client.post("/correct", json={"query": "hello", "max_results": 999})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_parameter"


def test_negative_threshold_rejected_by_schema(client):
    r = client.post("/correct", json={"query": "hello", "threshold": -1})
    assert r.status_code == 422


def test_uncertainties_returned_as_separate_field(client):
    r = client.post("/correct", json={"query": "the", "threshold": 2.0,
                                      "max_results": 1})
    body = r.json()
    assert isinstance(body["uncertainties"], list)
    assert any("only top 1 returned" in u for u in body["uncertainties"])


def test_diagnostics_documents_variant_and_bounds(client):
    r = client.get("/diagnostics")
    body = r.json()
    assert body["algorithm"]["restricted_OSA_used"] is False
    assert body["algorithm"]["lowrance_wagner_table_recurrence_used"] is False
    assert body["pruning"]["lossless_against_threshold"] is True
    assert body["ranking_key"] == "(distance asc, term asc, frequency desc)"
    assert set(body["pruning"]["dictionary_bounds"]) == {
        "directional length (min_delete/min_insert)",
        "0.5*L1(freq)*min_edit",
    }


def test_versions_lists_versions(client):
    r = client.get("/versions")
    body = r.json()
    assert body["active_version_id"] == body["versions"][0]["version_id"]
    assert body["versions"][0]["entry_count"] > 0
