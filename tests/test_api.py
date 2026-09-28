"""HTTP API tests via FastAPI TestClient (real ASGI stack, no network)."""
from __future__ import annotations

from tests.fixtures import builder as fb


def test_health_reports_versions(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "archiveguard"
    assert body["version"]
    assert body["python"].startswith("3.")


def _upload(client, path, data, filename="a.zip"):
    return client.post(path, files={"file": (filename, data, "application/octet-stream")})


def test_inspect_accepts_benign(client):
    r = _upload(client, "/api/v1/archives/inspect",
                fb.zip_bytes([{"name": "a.txt", "data": b"hi"}]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "accepted"
    assert body["plan"]["files"] == 1
    assert body["plan"]["container"] == "zip"
    assert body["run_id"]


def test_extract_returns_manifest(client):
    r = _upload(client, "/api/v1/archives/extract",
                fb.zip_bytes([{"name": "d/a.txt", "data": b"data"}]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "extracted"
    assert body["manifest"]["entries_written"] == 1
    assert body["output_dir"].endswith("output")


def test_rejection_is_specific_category_not_200(client):
    # Critical: failures must surface as an error status + exact category,
    # never as a success response.
    r = _upload(client, "/api/v1/archives/extract",
                fb.zip_bytes([{"name": "../escape", "data": b"x"}]))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["verdict"] == "rejected"
    assert detail["category"] == "path_escape"
    assert detail["evidence"]
    assert detail["run_id"]


def test_unsupported_format_status(client):
    r = _upload(client, "/api/v1/archives/extract", b"plain garbage bytes here")
    assert r.status_code == 415
    assert r.json()["detail"]["category"] == "unsupported_format"


def test_runs_and_events_endpoints(client):
    r = _upload(client, "/api/v1/archives/extract",
                fb.zip_bytes([{"name": "a.txt", "data": b"z"}]))
    run_id = r.json()["run_id"]

    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["run_id"] == run_id
    assert run["verdict"] == "extracted"

    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert any(e["stage"] == "extract_complete" for e in events)

    listing = client.get("/api/v1/runs").json()
    assert any(x["run_id"] == run_id for x in listing["runs"])


def test_audit_verify_endpoint(client):
    _upload(client, "/api/v1/archives/extract",
            fb.zip_bytes([{"name": "a.txt", "data": b"z"}]))
    r = client.get("/api/v1/audit/verify")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_missing_run_returns_404(client):
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404


def test_tar_hardlink_rejected_over_http(client):
    r = _upload(client, "/api/v1/archives/extract",
                fb.tar_bytes([{"name": "t", "data": b"x"},
                              {"name": "h", "hardlink": "t"}]),
                filename="a.tar")
    assert r.status_code == 422
    assert r.json()["detail"]["category"] == "hardlink_rejected"
