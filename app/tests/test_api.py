"""HTTP API tests (FastAPI TestClient) and metadata transaction checks."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.api.routes import create_app  # noqa: E402
from app.api.service import VerificationService  # noqa: E402
from app.metadata.store import MetadataStore  # noqa: E402


LIST_SCHEMA = {
    "name": "root", "type": "struct",
    "children": [
        {"name": "a", "type": "list",
         "item": {"name": "element", "type": "int32"}},
        {"name": "b", "type": "list",
         "item": {"name": "element", "type": "list",
                   "item": {"name": "element", "type": "int32"}}},
    ],
}


@pytest.fixture
def client(tmp_path):
    store = MetadataStore(tmp_path / "runs.db")
    svc = VerificationService(store=store, workroot=tmp_path / "work")
    app = create_app(svc)
    return TestClient(app)


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_verify_ok_and_request_id_correlation(client):
    body = {
        "schema": LIST_SCHEMA,
        "records": [{"a": [1, None], "b": [[1], []]}, {"a": [], "b": None}],
        "expected": [{"a": [1, None], "b": [[1], []]}, {"a": [], "b": None}],
        "page_slot_target": 2,
    }
    r = client.post("/api/v1/verify", json=body,
                    headers={"X-Request-ID": "rid-abc"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["request_id"] == "rid-abc"
    assert j["status"] == "OK"
    assert all(s["status"] == "PASS" for s in j["steps"])
    assert j["findings"] == []


def test_run_persisted_with_events(client):
    body = {"schema": LIST_SCHEMA,
            "records": [{"a": [1], "b": [[2]]}]}
    client.post("/api/v1/verify", json=body,
                headers={"X-Request-ID": "rid-persist"})
    g = client.get("/api/v1/runs/rid-persist").json()
    assert g["status"] == "OK"
    phases = [e["phase"] for e in g["events"]]
    assert "schema_admission" in phases
    assert any("level_oracle" in p for p in phases)


def test_values_and_levels_only_when_requested(client):
    body = {"schema": LIST_SCHEMA,
            "records": [{"a": [1], "b": [[2]]}]}
    plain = client.post("/api/v1/verify", json=body).json()
    assert plain["decoded"] is None and plain["kernel_levels"] is None
    full = client.post("/api/v1/verify", json=body,
                       params={"include": "values,levels"}).json()
    assert full["decoded"][0]["a"] == [1]
    assert "a.list.element" in full["kernel_levels"]


def test_unsupported_type_returns_structured_error(client):
    r = client.post("/api/v1/verify", json={
        "schema": {"name": "root", "type": "struct", "children": [
            {"name": "m", "type": "map"}]},
        "records": [],
    })
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "UNSUPPORTED_LOGICAL_TYPE"
    assert body["error"]["location"]["logical_type"] == "map"


def test_empty_struct_returns_structured_error(client):
    r = client.post("/api/v1/verify", json={
        "schema": {"name": "root", "type": "struct", "children": []},
        "records": [],
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "EMPTY_STRUCT"


def test_unknown_run_404(client):
    assert client.get("/api/v1/runs/nope").status_code == 404


def test_schema_admit_preview(client):
    r = client.post("/api/v1/schema/admit", json={"schema": LIST_SCHEMA})
    assert r.status_code == 200
    paths = {c["path"] for c in r.json()["leaf_columns"]}
    assert paths == {"a.list.element",
                     "b.list.element.list.element"}


def test_metadata_run_listing(client):
    for i in range(3):
        client.post("/api/v1/verify", json={
            "schema": LIST_SCHEMA,
            "records": [{"a": [i], "b": []}],
        }, headers={"X-Request-ID": f"rid-{i}"})
    runs = client.get("/api/v1/runs").json()["runs"]
    assert len(runs) == 3
    assert all(r["status"] == "OK" for r in runs)
