"""HTTP interface and SQLite job-state tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings
from app.jobs.manager import STATUS_DONE, STATUS_QUEUED


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "jobs.db"),
                        max_upload_bytes=64 * 1024)
    app = create_app(settings, start_worker=True)
    with TestClient(app) as c:
        yield c
    app.state.jobs.shutdown()


@pytest.fixture
def sample_ts(fixture_dir):
    return (fixture_dir / "baseline.ts").read_bytes()


def test_health_has_request_id(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("x-request-id")
    assert r.json()["request_id"]


def test_client_supplied_request_id_is_propagated(client, sample_ts):
    r = client.post("/api/v1/validate", content=sample_ts,
                    headers={"X-Request-ID": "fixed-rid-123",
                             "Content-Type": "video/mp2t"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "fixed-rid-123"
    assert r.json()["request_id"] == "fixed-rid-123"


def test_validate_returns_concrete_report(client, sample_ts):
    r = client.post("/api/v1/validate", content=sample_ts,
                    headers={"Content-Type": "video/mp2t"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "accepted"
    assert body["parsed_packets"] == 8
    assert "768" in body["programs"]


def test_empty_body_is_400_with_request_id(client):
    r = client.post("/api/v1/validate", content=b"")
    assert r.status_code == 400
    body = r.json()["detail"]
    assert body["error"] == "empty_body"
    assert body["request_id"]


def test_oversize_upload_is_413(client):
    r = client.post("/api/v1/validate", content=b"\x00" * 70000)
    assert r.status_code == 413
    assert r.json()["detail"]["error"] == "payload_too_large"


def test_async_job_lifecycle(client, sample_ts):
    r = client.post("/api/v1/jobs", content=sample_ts,
                    headers={"X-Request-ID": "job-rid",
                             "Content-Type": "video/mp2t"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.json()["request_id"] == "job-rid"

    status = client.get(f"/api/v1/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] in (STATUS_QUEUED, STATUS_DONE, "running")

    job = client.app.state.jobs.wait_for(job_id, timeout=10)
    assert job["status"] == STATUS_DONE
    assert job["report"]["verdict"] == "accepted"

    final = client.get(f"/api/v1/jobs/{job_id}")
    assert final.json()["report"]["programs"]["768"]["pmt_pid"] == 0x1000


def test_job_not_found_is_404(client):
    r = client.get("/api/v1/jobs/doesnotexist")
    assert r.status_code == 404
    body = r.json()
    # HTTPException bodies are nested under "detail"; app-level 404 handler
    # also includes the request id.
    assert body.get("detail", body)["error"] == "job_not_found"
    assert body.get("detail", body)["request_id"]


def test_jobs_persist_in_sqlite(client, sample_ts):
    r = client.post("/api/v1/jobs", content=sample_ts)
    job_id = r.json()["job_id"]
    client.app.state.jobs.wait_for(job_id, timeout=10)
    # Read directly from SQLite to confirm durable state, not just memory.
    import sqlite3
    db = client.app.state.jobs.db_path
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, error FROM jobs WHERE job_id=?",
            (job_id,)).fetchone()
    assert row[0] == STATUS_DONE and row[1] is None


def test_multipart_upload_job(client, sample_ts):
    r = client.post(
        "/api/v1/jobs/upload",
        files={"file": ("sensitive-name.ts", sample_ts, "video/mp2t")})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    job = client.app.state.jobs.wait_for(job_id, timeout=10)
    # Stored filename is masked, never logged in full.
    assert job["input_name"].startswith("s")
    assert "*" in job["input_name"]
    assert job["report"]["verdict"] == "accepted"
