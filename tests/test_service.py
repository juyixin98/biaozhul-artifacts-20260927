"""HTTP service tests via FastAPI's in-process test client (no network).

Both the success contract and the failure contract are asserted: errors
come back as HTTP 400 with the stable taxonomy code, never as 200/success.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from searchdsl.config import config_from_dict
from searchdsl.search import SearchEngine
from searchdsl.service import create_app
from searchdsl.store import Store

FIX = "fixtures"


@pytest.fixture
def client(tmp_path):
    cfg = config_from_dict(
        {"paths": {"schema": f"{FIX}/schema.json",
                   "corpus": f"{FIX}/corpus.jsonl",
                   "database": str(tmp_path / "t.db")}}
    )
    store = Store(":memory:")
    engine = SearchEngine(cfg, store=store)
    app = create_app(cfg, engine=engine)
    with TestClient(app) as c:
        yield c, engine


def test_health_reports_versions(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["versions"]["dsl_version"] == "dsl-1.0"
    assert body["versions"]["doc_count"] == 12
    assert len(body["versions"]["corpus_version"]) == 64


def test_schema_endpoint_lists_whitelist(client):
    c, _ = client
    fields = c.get("/schema").json()["fields"]
    assert set(fields) == {
        "title", "body", "notes", "category", "tags", "author", "year", "published"
    }
    assert fields["year"]["type"] == "int"
    assert fields["tags"]["multi_valued"] is True


def test_search_get_success_contract(client):
    c, _ = client
    r = c.get("/search", params={"q": "fox AND year:[2000 TO 2010]", "explain": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert [d["doc_id"] for d in body["results"]] == ["d01", "d03"]
    assert body["canonical"]["op"] == "and"
    assert body["query_hash"] and len(body["query_hash"]) == 64
    assert body["explain"]["op"] == "and"
    assert body["budget"]["clauses"] == 2
    # run correlation + versions present (run id may be supplied externally)
    assert isinstance(body["run_id"], str) and body["run_id"]
    summary_id = body["diagnostics"]["summary"]["run_id"]
    assert summary_id == body["run_id"]
    assert body["versions"]["corpus_version"]
    stages = {e["stage"] for e in body["diagnostics"]["events"]}
    assert {"input", "parse", "validate", "normalize", "execute"} <= stages


def test_search_post_matches_get(client):
    c, _ = client
    r = c.post("/search", json={"q": "tags:winter", "limit": 5})
    assert r.status_code == 200
    assert [d["doc_id"] for d in r.json()["results"]] == ["d03", "d07"]


def test_syntax_error_is_400_with_code_and_position(client):
    c, _ = client
    r = c.get("/search", params={"q": "title:"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "UNEXPECTED_TOKEN"
    assert detail["pos"] == {"start": 0, "end": 6}


def test_unknown_field_is_400_field_unknown(client):
    c, _ = client
    r = c.get("/search", params={"q": "ghost:cat"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "FIELD_UNKNOWN"


def test_budget_violation_is_400_not_success(client):
    c, engine = client
    engine.config = engine.config.with_overrides(limits={"max_clauses": 2})
    r = c.get("/search", params={"q": "a b c"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "BUDGET_CLAUSES"


def test_empty_query_is_400_query_empty(client):
    c, _ = client
    r = c.get("/search", params={"q": "   "})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "QUERY_EMPTY"


def test_unterminated_string_is_400_with_span(client):
    c, _ = client
    r = c.get("/search", params={"q": '"abc'})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["code"] == "UNTERMINATED_STRING"
    assert d["pos"]["start"] == 0


def test_saved_query_roundtrip(client):
    c, _ = client
    r = c.get("/search", params={"q": "fox OR dog"})
    qhash = r.json()["query_hash"]
    saved = c.get(f"/searches/{qhash}")
    assert saved.status_code == 200
    assert saved.json()["query_hash"] == qhash
    assert saved.json()["corpus_version"]
    assert c.get("/searches/" + "0" * 64).status_code == 404


def test_identical_queries_share_hash(client):
    c, _ = client
    h1 = c.get("/search", params={"q": "fox dog"}).json()["query_hash"]
    h2 = c.get("/search", params={"q": "dog AND fox"}).json()["query_hash"]
    assert h1 == h2
