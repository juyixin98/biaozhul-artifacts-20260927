"""End-to-end HTTP tests via FastAPI's in-process ASGI client (TestClient)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "jobs.db"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health_reports_versions_and_config(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["numpy"] and body["sqlite"]
    assert body["config"]["min_duration_ms"] == 1000


def test_validate_clean_document(client):
    content = (FIX / "clean.srt").read_text()
    r = client.post("/api/v1/validate", json={"content": content})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "clean"
    assert body["cue_count"] == 4
    assert body["diagnostics"] == []
    assert body["repaired_document"] is None
    assert body["run_id"].startswith("run-")


def test_validate_chain_overlap_returns_repaired_document(client):
    content = (FIX / "chain_overlap.srt").read_text()
    r = client.post("/api/v1/validate", json={"content": content})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "repaired"
    assert body["repair"]["total_shift_ms"] >= 0
    assert body["repaired_document"].count("-->") == 5
    # reasons reference concrete diagnostic codes
    moved = [c for c in body["repair"]["cues"] if c["shift_ms"] != 0]
    assert moved and "overlap" in moved[0]["reasons"]


def test_validate_parse_failure_is_200_with_explicit_status(client):
    content = (FIX / "bad_timestamp.srt").read_text()
    r = client.post("/api/v1/validate", json={"content": content})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "parse_failed"
    assert "invalid_timestamp" in body["failure_codes"]
    assert body["repaired_document"] is None


def test_validate_unfixable_returns_failure_category(client, tmp_path):
    # A genuinely unfixable document: two 3000ms cues overlap inside the first
    # 5s segment, and a 300ms per-cue cap cannot separate them.
    doc = (
        "1\n00:00:01,000 --> 00:00:04,000\na\n\n"
        "2\n00:00:01,500 --> 00:00:04,500\nb\n"
    )
    app = create_app(Settings(db_path=str(tmp_path / "tight.db"),
                              segment_boundaries_ms=(5_000, 60_000),
                              max_per_cue_shift_ms=300))
    with TestClient(app) as tight:
        r = tight.post("/api/v1/validate", json={"content": doc})
    body = r.json()
    assert body["status"] == "infeasible_bounds"
    assert body["failure_codes"] == ["infeasible_bounds"]
    assert body["repair"] is None
    assert body["repaired_document"] is None


def test_validate_bad_request_envelope(client):
    r = client.post("/api/v1/validate", json={"content": ""})
    assert r.status_code == 400
    assert r.json()["error_code"] == "INVALID_REQUEST"

    r2 = client.post("/api/v1/validate", json={"content": "x", "format": "pdf"})
    assert r2.status_code == 422


def test_jobs_lifecycle_and_persistence(client):
    content = (FIX / "chain_overlap.srt").read_text()
    r = client.post("/api/v1/jobs", json={"content": content})
    assert r.status_code == 201
    job_id = r.json()["job_id"]
    assert r.json()["url"] == f"/api/v1/jobs/{job_id}"

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] == "succeeded"
    assert detail["cue_count"] == 5
    assert detail["result"]["status"] == "repaired"
    assert detail["repaired_document"].count("-->") == 5

    listing = client.get("/api/v1/jobs").json()
    assert any(j["job_id"] == job_id for j in listing)

    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404
    body = client.get("/api/v1/jobs/does-not-exist").json()
    assert body["error_code"] == "JOB_NOT_FOUND"


def test_job_for_parse_failure_terminal_status(client):
    content = (FIX / "bad_markup.vtt").read_text()
    r = client.post("/api/v1/jobs", json={"content": content, "format": "vtt"})
    job_id = r.json()["job_id"]
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] == "parse_failed"
    codes = {d["code"] for d in detail["result"]["diagnostics"]}
    assert "unsupported_markup" in codes
    assert detail["repaired_document"] is None


def test_vtt_forced_format(client):
    doc = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello vtt\n"
    r = client.post("/api/v1/validate", json={"content": doc, "format": "vtt"})
    assert r.json()["format"] == "vtt"
    assert r.json()["status"] == "clean"
