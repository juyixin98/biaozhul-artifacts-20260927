"""HTTP interface tests: request correlation, error categories, explainability."""

from __future__ import annotations

import dataclasses

import pytest

httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from zcluster.api.app import create_app
from zcluster.config import Config


@pytest.fixture
def client(base_config):
    app = create_app(base_config)
    with TestClient(app) as c:
        yield c


def test_health_reports_versions(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(["coder_version", "chunk_format_version", "checks_version"]).issubset(body)
    assert body["dataset_initialized"] is False


def test_request_id_is_echoed_and_generated(client):
    r = client.get("/health", headers={"X-Request-Id": "fixed-rid-1"})
    assert r.headers["x-request-id"] == "fixed-rid-1"
    r2 = client.get("/health")
    assert r2.headers["x-request-id"].startswith("req-")


def test_schema_ingest_query_flow_with_explainability(client):
    dims = [{"name": "x", "bits": 4, "signed": True},
            {"name": "y", "bits": 3, "signed": False}]
    r = client.post("/api/schema", json={"name": "demo", "dimensions": dims},
                    headers={"X-Request-Id": "rid-schema"})
    assert r.status_code == 200
    assert r.json()["request_id"] == "rid-schema"
    assert r.json()["schema"]["total_interleaved_bits"] == 8

    rows = [{"x": -8, "y": 0}, {"x": -1, "y": 2}, {"x": 0, "y": 2},
            {"x": 7, "y": 7}, {"x": 3, "y": 1}]
    r = client.post("/api/ingest", json={"rows": rows})
    assert r.status_code == 200
    assert r.json()["ingest"]["accepted"] == 5

    body_query = {"box": [{"dimension": "x", "lo": -1, "hi": 0},
                          {"dimension": "y", "lo": 2, "hi": 2}]}
    r = client.post("/api/query", json=body_query,
                    headers={"X-Request-Id": "rid-query"})
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == "rid-query"
    got = sorted((row["row_id"], row["x"], row["y"]) for row in body["rows"])
    assert got == [(1, -1, 2), (2, 0, 2)]
    # explainable: signed->unsigned edges shown, steps present, stats present
    assert body["box"]["unsigned"] == [[7, 8], [2, 2]]
    assert body["steps"] and any("unsigned" in s for s in body["steps"])
    for key in ("intervals", "chunks_read", "candidate_rows",
                "result_rows", "false_positive_rows", "bytes_read"):
        assert key in body["stats"] or key in body
    assert body["budget_exhausted"] is False

    # request correlation persisted
    r = client.get("/api/requests/rid-query")
    assert r.status_code == 200
    assert r.json()["detail"]["stats"]["result_rows"] == 2


def test_error_categories_are_stable_and_classified(client, base_config):
    # query before init
    r = client.post("/api/query",
                    json={"box": [{"dimension": "x", "lo": 0, "hi": 1}]})
    assert r.status_code == 409
    assert r.json()["error_category"] == "not_initialized"

    client.post("/api/schema",
                json={"name": "d", "dimensions": [{"name": "x", "bits": 4}]})
    # re-init
    r = client.post("/api/schema",
                    json={"name": "d", "dimensions": [{"name": "x", "bits": 4}]})
    assert r.status_code == 409
    assert r.json()["error_category"] == "already_initialized"

    # invalid dimension spec: pydantic field validation (bits >= 1)
    bad_cfg = dataclasses.replace(base_config,
                                  data_root=base_config.data_root + "-bad")
    with TestClient(create_app(bad_cfg)) as bad_client:
        r = bad_client.post(
            "/api/schema",
            json={"name": "d", "dimensions": [{"name": "x", "bits": 0}]},
        )
        assert r.status_code == 422
        r = bad_client.post(
            "/api/schema",
            json={"name": "d", "dimensions": [{"name": "", "bits": 4}]},
        )
        assert r.status_code == 422

    # out-of-domain coordinates
    r = client.post("/api/ingest", json={"rows": [{"x": 9999}]})
    assert r.status_code == 400
    assert r.json()["error_category"] == "coordinate_out_of_domain"

    # malformed box
    r = client.post("/api/query",
                    json={"box": [{"dimension": "z", "lo": 0, "hi": 1}]})
    assert r.status_code == 400
    assert r.json()["error_category"] == "query_validation_error"

    # invalid budget
    r = client.post("/api/query",
                    json={"box": [{"dimension": "x", "lo": 0, "hi": 1}],
                          "interval_budget": 0})
    assert r.status_code == 422  # pydantic field validation


def test_budget_escape_is_listed_as_uncertainty(client):
    client.post("/api/schema",
                json={"name": "d",
                      "dimensions": [{"name": "x", "bits": 4},
                                     {"name": "y", "bits": 4}]})
    rows = [{"x": i % 16, "y": i // 16} for i in range(64)]
    client.post("/api/ingest", json={"rows": rows})
    r = client.post("/api/query",
                    json={"box": [{"dimension": "x", "lo": 3, "hi": 12},
                                  {"dimension": "y", "lo": 3, "hi": 12}],
                          "interval_budget": 1})
    body = r.json()
    assert body["budget_exhausted"] is True
    assert body["uncertainties"]
    assert any("budget" in u for u in body["uncertainties"])


def test_verify_endpoint_runs_suite_on_isolated_fixtures(client):
    r = client.post("/api/verify", headers={"X-Request-Id": "rid-verify"})
    assert r.status_code == 200
    report = r.json()["report"]
    assert report["ok"] is True, report["failure_categories"]
    names = [c["name"] for c in report["checks"]]
    assert names == ["roundtrip", "box_coverage", "zero_miss", "rowid_stability"]
    for c in report["checks"]:
        assert c["failure_category"] is None


def test_chunks_endpoint_and_compaction(client):
    client.post("/api/schema",
                json={"name": "d",
                      "dimensions": [{"name": "x", "bits": 3, "signed": True},
                                     {"name": "y", "bits": 3}]})
    client.post("/api/ingest", json={"rows": [{"x": i, "y": i % 8}
                                              for i in range(-4, 4)]})
    r = client.get("/api/chunks")
    assert r.status_code == 200
    assert len(r.json()["chunks"]) >= 2
    r = client.post("/api/compact")
    assert r.status_code == 200
    assert r.json()["compact"]["stats"]["row_ids_preserved"] is True
