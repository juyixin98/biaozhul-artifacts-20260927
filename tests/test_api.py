"""HTTP-level tests against the FastAPI app using an in-process client."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PNV_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("PNV_ARTIFACT_DIR", str(tmp_path / "art"))
    # Import fresh so the env-bound settings/store take effect.
    import importlib
    import app.config as config_mod
    importlib.reload(config_mod)
    import app.storage.metadata as metadata_mod
    importlib.reload(metadata_mod)
    import app.service as service_mod
    importlib.reload(service_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    return TestClient(main_mod.app)


def _body(**over):
    body = {
        "schema": {
            "name": "api_case",
            "fields": [
                {"name": "id", "type": "int64", "repetition": "required"},
                {"name": "ids", "type": "list", "repetition": "optional",
                 "element": {"type": "list", "element": {"type": "int32"}}},
            ],
        },
        "records": [
            {"id": 1, "ids": None},
            {"id": 2, "ids": []},
            {"id": 3, "ids": [None, [1, 2], [3]]},
            {"id": 4, "ids": [[4], [5]]},
        ],
        "force_page_after_records": 2,
    }
    body.update(over)
    return body


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_validate_passes_and_correlates_request_id(client):
    r = client.post("/api/v1/validate", json=_body(request_id="abc-123"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["request_id"] == "abc-123"
    assert data["status"] == "passed"
    assert data["mismatches"] == []
    assert data["page_count"] >= 2
    steps = [s["name"] for s in data["steps"]]
    assert "cross_interop" in steps


def test_request_id_generated_when_absent(client):
    r = client.post("/api/v1/validate", json=_body())
    rid = r.json()["request_id"]
    assert rid.startswith("req-")
    r2 = client.get(f"/api/v1/requests/{rid}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "passed"


def test_unsupported_type_rejected_over_http(client):
    body = _body(schema={"fields": [{"name": "x", "type": "timestamp"}]},
                 records=[])
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "error"
    assert data["error_category"] == "UNSUPPORTED_LOGICAL_TYPE"


def test_unknown_request_404(client):
    r = client.get("/api/v1/requests/does-not-exist")
    assert r.status_code == 404


def test_list_requests(client):
    client.post("/api/v1/validate", json=_body(request_id="z1"))
    r = client.get("/api/v1/requests")
    ids = [x["request_id"] for x in r.json()["requests"]]
    assert "z1" in ids
