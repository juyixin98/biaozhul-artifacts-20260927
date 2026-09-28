"""API tests: health/versions, job lifecycle, validation endpoint, and the
guarantee that failures never surface as success."""

import logging

import pytest
from fastapi.testclient import TestClient

from fixtures import expected
from mp4timeline.api import create_app
from mp4timeline.config import SERVICE_VERSION, Settings

from .conftest import fixture_path

log = logging.getLogger("mp4timeline.tests")


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "api_jobs.db")))
    with TestClient(app) as c:
        yield c


def test_health_reports_versions(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    log.info("health: %s", body)
    assert body["service_version"] == SERVICE_VERSION
    assert body["numpy"] and body["fastapi"] and body["python"]


def test_job_lifecycle_success(client, run_identity):
    r = client.post("/jobs", json={"path": fixture_path("multitrack.mp4")})
    assert r.status_code == 201
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "done"
    log.info("RUN=%s job=%s done", run_identity, job_id)

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "done"
    assert any(e["event"] == "boxes_parsed" for e in job["events"])

    tl = client.get(f"/jobs/{job_id}/timeline").json()
    assert tl["movie_timescale"] == 1000
    assert len(tl["tracks"]) == 2
    audio = tl["tracks"][1]
    assert audio["presentations"][0]["movie_end"] == {"num": 64, "den": 3}
    # result is bound to the input identity
    assert tl["source"]["sha256"] == job["input_sha256"]


@pytest.mark.parametrize("name", sorted(expected.BAD_FIXTURES))
def test_job_failure_is_classified_not_success(client, name, run_identity):
    category = expected.BAD_FIXTURES[name]
    r = client.post("/jobs", json={"path": fixture_path(name)})
    assert r.status_code == 201  # job record created...
    body = r.json()
    log.info("RUN=%s case=%s status=%s", run_identity, name, body["status"])
    assert body["status"] == "failed"                      # ...but never "done"
    assert body["error"]["category"] == category

    job_id = body["job_id"]
    r = client.get(f"/jobs/{job_id}/timeline")
    assert r.status_code == 409  # no timeline for a failed job


def test_unknown_job_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/timeline").status_code == 404


def test_validate_endpoint_ok(client):
    r = client.post("/validate", json={"path": fixture_path("trim.mp4")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["tracks"][0]["presentations"] == 1
    assert body["input_sha256"]


@pytest.mark.parametrize("name", sorted(expected.BAD_FIXTURES))
def test_validate_endpoint_rejects_with_category(client, name):
    r = client.post("/validate", json={"path": fixture_path(name)})
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["category"] == expected.BAD_FIXTURES[name]


def test_validate_missing_file(client):
    r = client.post("/validate", json={"path": "/nonexistent.mp4"})
    assert r.status_code == 422
    assert r.json()["category"] == "input_error"
