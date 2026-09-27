"""End-to-end HTTP tests using FastAPI's in-process test client.

These cover the full path: request validation -> engine -> diagnostics ->
SQLite persistence -> explicit rebuild.  Expected merged strings come from
the same hand-authored literals as the core tests, not from the engine.
"""

import pytest
from fastapi.testclient import TestClient

from merge3.api import create_app
from merge3.config import Settings
from merge3.storage import VersionStore


@pytest.fixture()
def client():
    settings = Settings(database_path=":memory:", max_document_chars=100_000)
    store = VersionStore(":memory:")
    app = create_app(store=store, settings=settings)
    with TestClient(app) as c:
        c._store = store
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_clean_merge_returns_exact_text_and_correlation_id(client):
    resp = client.post("/v1/merges", json={
        "request_id": "req-clean",
        "base_text": "p1\np2\np3\np4\np5\np6\n",
        "local_text": "p2\np1\np3\np4\np5\np6\n",
        "remote_text": "p1\np2\np3\np4\np5\nP6SIX\ntail\n",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "auto"
    assert body["request_id"] == "req-clean"
    assert body["merged_text"] == "p2\np1\np3\np4\np5\nP6SIX\ntail\n"
    assert body["conflicts"] == []
    # diagnostics must explain acceptance with the same request id
    events = body["diagnostics"]
    assert any(e["event"] == "merge_accepted" and
               e["request_id"] == "req-clean" for e in events)
    assert any("no choice" in e["reason"] for e in events)


def _create_delete_modify_merge(client) -> str:
    resp = client.post("/v1/merges", json={
        "request_id": "req-dm",
        "base_text": "alpha\nbeta\ngamma\ndelta\n",
        "local_text": "alpha\ngamma\ndelta\n",
        "remote_text": "alpha\nBETA!\ngamma\ndelta\n",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "conflict"
    assert body["merged_text"] is None
    (conflict,) = body["conflicts"]
    assert conflict["conflict_type"] == "delete_modify"
    assert conflict["base_region"]["line_start"] == 1
    assert conflict["local_region"]["end"] == conflict["local_region"]["start"]
    assert conflict["remote_text"] == "BETA!\n"
    assert "custom_text" in conflict["allowed_resolutions"]
    events = body["diagnostics"]
    ind = [e for e in events if e["event"] == "merge_indeterminate"]
    assert ind and "will not guess" in ind[0]["reason"]
    return body["merge_id"]


def test_conflict_carries_three_way_ranges_and_blocks_no_text(client):
    _create_delete_modify_merge(client)


def test_resolve_endpoint_rebuilds_by_explicit_choice(client):
    mid = _create_delete_modify_merge(client)
    resp = client.post(f"/v1/merges/{mid}/resolve", json={
        "resolutions": {"c1": {"choice": "remote"}}
    })
    assert resp.status_code == 200
    assert resp.json()["merged_text"] == "alpha\nBETA!\ngamma\ndelta\n"

    # and the other choice reconstructs the other side
    resp2 = client.post(f"/v1/merges/{mid}/resolve", json={
        "resolutions": {"c1": {"choice": "local"}}
    })
    assert resp2.json()["merged_text"] == "alpha\ngamma\ndelta\n"


def test_same_point_insert_ordering_choices(client):
    r = client.post("/v1/merges", json={
        "base_text": "h1\nh2\nh3\n",
        "local_text": "INS-L\nh1\nh2\nh3\n",
        "remote_text": "INS-R\nh1\nh2\nh3\n",
    })
    mid = r.json()["merge_id"]
    for choice, expected in [
        ("local", "INS-L\nh1\nh2\nh3\n"),
        ("remote", "INS-R\nh1\nh2\nh3\n"),
        ("local_then_remote", "INS-L\nINS-R\nh1\nh2\nh3\n"),
        ("remote_then_local", "INS-R\nINS-L\nh1\nh2\nh3\n"),
        ("base", "h1\nh2\nh3\n"),
    ]:
        rr = client.post(f"/v1/merges/{mid}/resolve", json={
            "resolutions": {"c1": {"choice": choice}}})
        assert rr.status_code == 200, (choice, rr.text)
        assert rr.json()["merged_text"] == expected


def test_custom_text_resolution(client):
    r = client.post("/v1/merges", json={
        "base_text": "a\nb\nc\n",
        "local_text": "a\nX\nc\n",
        "remote_text": "a\nY\nc\n",
    })
    mid = r.json()["merge_id"]
    rr = client.post(f"/v1/merges/{mid}/resolve", json={
        "resolutions": {"c1": {"choice": "custom_text", "text": "BOTH\n"}}})
    assert rr.json()["merged_text"] == "a\nBOTH\nc\n"


def test_resolution_failures_have_named_categories(client):
    r = client.post("/v1/merges", json={
        "base_text": "a\nb\n",
        "local_text": "a\nX\n",
        "remote_text": "a\nY\n",
    })
    mid = r.json()["merge_id"]
    # missing resolution
    rr = client.post(f"/v1/merges/{mid}/resolve", json={"resolutions": {}})
    assert rr.status_code == 409
    assert rr.json()["error"] == "resolution_invalid"
    # illegal choice
    rr = client.post(f"/v1/merges/{mid}/resolve", json={
        "resolutions": {"c1": {"choice": "flip"}}})
    assert rr.status_code == 409
    # unknown merge
    rr = client.post("/v1/merges/mg_missing/resolve", json={
        "resolutions": {"c1": {"choice": "local"}}})
    assert rr.status_code == 404
    assert rr.json()["error"] == "not_found"


def test_input_validation_categories(client):
    rr = client.post("/v1/merges", json={
        "base_text": 123, "local_text": "x", "remote_text": "y"})
    assert rr.status_code == 422  # pydantic type validation

    big = "x" * 100_001
    rr = client.post("/v1/merges", json={
        "base_text": big, "local_text": "x", "remote_text": "y"})
    assert rr.status_code == 413
    assert rr.json()["error"] == "payload_too_large"


def test_eol_fidelity_through_the_http_layer(client):
    rr = client.post("/v1/merges", json={
        "base_text": "one\r\ntwo\r\nthree\r\n",
        "local_text": "one\r\nTWO\r\nthree\r\n",
        "remote_text": "one\r\ntwo\r\nthree\r\nfour\r\n",
    })
    assert rr.json()["merged_text"] == "one\r\nTWO\r\nthree\r\nfour\r\n"
    # the stored merged version must be byte-identical too
    versions = client.get("/v1/documents/default/versions").json()["versions"]
    merged = [v for v in versions if v["role"] == "merged"][0]
    assert client._store.get_version(merged["version_id"])["content"] == \
        "one\r\nTWO\r\nthree\r\nfour\r\n"


def test_get_merge_record_exposes_provenance(client):
    mid = _create_delete_modify_merge(client)
    rec = client.get(f"/v1/merges/{mid}").json()
    assert rec["status"] == "conflict"
    assert rec["conflicts"][0]["conflict_type"] == "delete_modify"
