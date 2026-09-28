"""End-to-end HTTP tests against the real ASGI app (no network/server)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=tmp_path / "api.db",
        seed_path=tmp_path / "nonexistent-seed.json",
        auto_seed=False,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


SEED = [
    {"surface": "研究", "frequency": 100},
    {"surface": "研究生", "frequency": 50000},
    {"surface": "生命", "frequency": 90},
    {"surface": "命", "frequency": 15},
]


def _publish(client, entries, note="t"):
    resp = client.post("/admin/dictionaries", json={"entries": entries, "note": note})
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]


def test_health_before_and_after_publish(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"
    _publish(client, SEED)
    resp = client.get("/health")
    assert resp.json()["status"] == "ok"
    assert resp.json()["word_count"] == 4


def test_segment_full_contract(client):
    version = _publish(client, SEED)
    resp = client.post("/segment", json={"text": "研究生命"})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["version"] == version
    assert body["request_id"]
    assert [t["surface"] for t in body["tokens"]] == ["研究生", "命"]
    for tok in body["tokens"]:
        assert tok["orig_end"] >= tok["orig_start"]
    assert body["coverage"]["orig_covered"] is True
    assert body["coverage"]["reconstructed"] is True
    assert body["runner_up"]["surfaces"] == ["研究", "生命"]
    assert body["gap_status"] == "available"
    assert isinstance(body["gap"], float)
    # Diagnostics are present and structured.
    stages = [e["stage"] for e in body["diagnostics"]["events"]]
    assert stages == ["version_resolution", "normalization", "dag_search"]


def test_request_id_can_be_supplied_and_is_echoed(client):
    _publish(client, SEED)
    resp = client.post("/segment", json={"text": "研究"},
                       headers={"X-Request-Id": "fixed-123"})
    assert resp.json()["request_id"] == "fixed-123"
    assert resp.json()["diagnostics"]["request_id"] == "fixed-123"


def test_pinned_version_survives_republish(client):
    v1 = _publish(client, [
        {"surface": "研究", "frequency": 100},
        {"surface": "生命", "frequency": 90},
        {"surface": "研究生", "frequency": 50},
    ], note="v1")
    v2 = _publish(client, SEED, note="v2")

    # Pin v1 via header; later publish already done -> still gets v1 answer.
    resp = client.post("/segment", json={"text": "研究生命"},
                       headers={"X-Dictionary-Version": v1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == v1
    assert [t["surface"] for t in body["tokens"]] == ["研究", "生命"]

    # Default follows current (v2).
    resp = client.post("/segment", json={"text": "研究生命"})
    assert resp.json()["version"] == v2


def test_pinned_unknown_version_returns_409(client):
    _publish(client, SEED)
    resp = client.post("/segment", json={"text": "研究"},
                       headers={"X-Dictionary-Version": "v-nope"})
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "version_not_found"
    assert err["request_id"]
    assert err["details"]["requested"] == "v-nope"


def test_missing_text_is_422_with_envelope(client):
    _publish(client, SEED)
    resp = client.post("/segment", json={})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "invalid_request"
    assert err["details"][0]["location"] == ["body", "text"]


def test_publish_empty_batch_is_400_with_codes(client):
    resp = client.post("/admin/dictionaries", json={"entries": []})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "invalid_dictionary"
    assert err["details"]["issues"][0]["code"] == "empty_batch"


def test_publish_invalid_entries_no_version_created(client):
    bad = client.post("/admin/dictionaries", json={"entries": [
        {"surface": ""},
        {"surface": "a", "frequency": -1},
    ]})
    assert bad.status_code == 400
    codes = {i["code"] for i in bad.json()["error"]["details"]["issues"]}
    assert {"surface_empty", "frequency_invalid"} <= codes

    versions = client.get("/admin/dictionaries").json()["versions"]
    assert versions == []


def test_versions_listing_retains_history(client):
    v1 = _publish(client, [{"surface": "a", "frequency": 1}])
    v2 = _publish(client, SEED)
    versions = client.get("/admin/dictionaries").json()["versions"]
    ids = [v["version"] for v in versions]
    assert v1 in ids and v2 in ids
    current = [v for v in versions if v["is_current"]]
    assert len(current) == 1 and current[0]["version"] == v2


def test_empty_text_succeeds_with_empty_tokens(client):
    _publish(client, SEED)
    resp = client.post("/segment", json={"text": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tokens"] == []
    assert body["gap_status"] == "unique_path"
    assert body["coverage"]["orig_covered"] is True


def test_oov_never_drops_characters(client):
    _publish(client, [{"surface": "研究", "frequency": 100}])
    resp = client.post("/segment", json={"text": "研究qz"})
    body = resp.json()
    assert [t["surface"] for t in body["tokens"]] == ["研究", "q", "z"]
    assert [t["kind"] for t in body["tokens"]] == ["dict", "unknown", "unknown"]
    # Ranges tile [0, 3).
    assert body["coverage"]["orig_ranges"] == [[0, 2], [2, 3], [3, 4]]
    assert body["coverage"]["reconstructed"] is True
