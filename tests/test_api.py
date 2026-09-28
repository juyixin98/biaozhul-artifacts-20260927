"""HTTP interface tests: concrete results, failure categories and auditability."""
from __future__ import annotations

import pytest

pytest.importorskip("httpx")
from starlette.testclient import TestClient

from zindex.api import create_app
from zindex.chunkstore import chunk_path


@pytest.fixture()
def client(tmp_env):
    app = create_app(tmp_env["settings"])
    with TestClient(app) as c:
        yield c


DIMS = [
    {"name": "x", "bits": 5, "signed": True},
    {"name": "y", "bits": 5, "signed": True},
]


def test_full_request_lifecycle_envelope(client):
    r = client.post("/schemas", json={"name": "s1", "dims": DIMS})
    assert r.status_code == 201
    body = r.json()
    rid = body["request_id"]
    assert body["status"] == "complete"
    assert body["engine_version"] == "morton-z-v1"
    assert r.headers["x-request-id"] == rid

    r = client.post("/schemas/s1/ingest", json={"rows": [[0, 0], [-1, -1], [2, 3]]})
    assert r.status_code == 201
    assert r.json()["data"]["rows_ingested"] == 3

    r = client.post("/schemas/s1/query", json={"lo": [-1, -1], "hi": [-1, 3]})
    q = r.json()
    assert q["status"] == "complete"
    # only row 1 (-1,-1) matches; (0,0) and (2,3) are outside x=[-1,-1]
    assert [(row["__row_id"], row["x"], row["y"]) for row in q["data"]["rows"]] == [
        (1, -1, -1),
    ]
    assert q["stats"]["exact_matches"] == 1
    step_names = [s["step"] for s in q["steps"]]
    assert step_names == ["box_mapped", "box_decomposed", "chunks_pruned", "chunk_scanned"]
    # decomposition step carries version/shape details
    dec = next(s for s in q["steps"] if s["step"] == "box_decomposed")
    assert dec["budget_exhausted"] is False
    assert dec["num_intervals"] >= 1

    # audit trail keyed by the same request identity
    audit = client.get(f"/requests/{rid}").json()["data"]
    assert audit["request_id"] == rid
    assert audit["kind"] == "schema_create"
    assert audit["status"] == "complete"


def test_error_categories_are_specific_not_generic(client):
    client.post("/schemas", json={"name": "s2", "dims": DIMS})
    cases = [
        ("POST", "/schemas/s2/query", {"lo": [0], "hi": [1]}, 400, "invalid_box"),
        ("POST", "/schemas/s2/query", {"lo": [0, 0], "hi": [99, 0]}, 400, "invalid_box"),
        ("POST", "/schemas/missing/query", {"lo": [0, 0], "hi": [1, 1]}, 404, "schema_not_found"),
        ("POST", "/schemas/s2/ingest", {"rows": [[100, 0]]}, 400, "invalid_coordinate"),
        ("POST", "/schemas/s2/ingest", {"rows": [[0]]}, 400, "invalid_coordinate"),
    ]
    for method, path, payload, want_status, want_cat in cases:
        r = client.request(method, path, json=payload)
        assert r.status_code == want_status, (path, r.text)
        body = r.json()
        assert body["status"] == "error"
        assert body["errors"][0]["category"] == want_cat, (path, body["errors"])
        # failures are audited too
        audit = client.get(f"/requests/{body['request_id']}").json()["data"]
        assert audit["status"] == "error"
        assert audit["http_status"] == want_status


def test_schema_conflict_and_replace(client):
    r = client.post("/schemas", json={"name": "s3", "dims": DIMS})
    assert r.status_code == 201
    r = client.post("/schemas", json={"name": "s3", "dims": DIMS})
    assert r.status_code == 409
    assert r.json()["errors"][0]["category"] == "schema_exists"
    # replace on a missing schema is 404, not a silent create
    r = client.put("/schemas/ghost", json={"dims": DIMS})
    assert r.status_code == 404
    # replacing wipes old chunks and keeps serving
    client.post("/schemas/s3/ingest", json={"rows": [[1, 1]]})
    r = client.put("/schemas/s3", json={"dims": [
        {"name": "a", "bits": 4, "signed": True},
        {"name": "b", "bits": 4, "signed": True},
    ]})
    assert r.status_code == 200
    spec = client.get("/schemas/s3").json()["data"]
    assert [d["name"] for d in spec["spec"]["dims"]] == ["a", "b"]
    assert spec["chunks"] == []


def test_budget_exhaustion_is_an_uncertainty_not_an_error(client):
    client.post("/schemas", json={"name": "s4", "dims": [
        {"name": "x", "bits": 8, "signed": True},
        {"name": "y", "bits": 8, "signed": True},
    ]})
    client.post("/schemas/s4/ingest_synthetic", json={"n": 2000, "shape": "uniform", "seed": 1, "capacity": 500})
    r = client.post("/schemas/s4/query", json={"lo": [-40, -40], "hi": [40, 40], "max_intervals": 1})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "complete"
    cats = [u["category"] for u in body["uncertainties"]]
    assert "budget_exhausted" in cats
    assert body["stats"]["budget_exhausted"] is True
    assert body["stats"]["code_candidates"] >= body["stats"]["exact_matches"]


def test_rewrite_endpoint_preserves_ids(client):
    client.post("/schemas", json={"name": "s5", "dims": DIMS})
    client.post("/schemas/s5/ingest_synthetic", json={"n": 1500, "shape": "uniform", "seed": 8, "capacity": 400})
    before = client.post("/schemas/s5/query", json={"lo": [-16, -16], "hi": [15, 15]}).json()
    rw = client.post("/schemas/s5/rewrite", json={"capacity": 600}).json()["data"]
    assert rw["row_id_preserved"] is True
    assert rw["rows_rewritten"] == 1500
    after = client.post("/schemas/s5/query", json={"lo": [-16, -16], "hi": [15, 15]}).json()
    assert before["data"]["rows"] == after["data"]["rows"]
    assert before["stats"]["exact_matches"] == after["stats"]["exact_matches"] == 1500


def test_missing_chunk_file_degrades_response(client, tmp_env):
    client.post("/schemas", json={"name": "s6", "dims": DIMS})
    client.post("/schemas/s6/ingest_synthetic", json={"n": 900, "shape": "uniform", "seed": 2, "capacity": 300})
    target = chunk_path(tmp_env["data_dir"], "s6", 0)
    target.unlink()
    r = client.post("/schemas/s6/query", json={"lo": [-16, -16], "hi": [15, 15]})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "degraded"
    unc = [u for u in body["uncertainties"] if u["category"] == "chunk_unreadable"]
    assert len(unc) == 1
    assert unc[0]["chunk_id"] == 0
    assert "path" in unc[0] and "reason" in unc[0]


def test_inbound_request_id_is_honored(client):
    r = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["x-request-id"] == "trace-abc-123"
    assert client.get("/requests/trace-abc-123").json()["data"]["request_id"] == "trace-abc-123"
