"""API integration tests via FastAPI TestClient."""

import pytest
from fastapi.testclient import TestClient

from driftcorr.api.app import create_app

TRUE_OFFSET_S = 0.123
TRUE_DRIFT_PPM = 75.0


@pytest.fixture()
def client(cfg):
    return TestClient(create_app(cfg))


def _post_clean(client, scn, request_id="req-it-001"):
    return client.post(
        "/jobs",
        json={
            "reference_path": scn["reference_wav"],
            "target_path": scn["target_wav"],
            "reference_metadata_path": scn["reference_meta"],
            "target_metadata_path": scn["target_meta"],
        },
        headers={"X-Request-ID": request_id},
    )


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["pipeline_version"].startswith("driftcorr-pipeline/")


def test_job_lifecycle_and_report(client, scenarios):
    r = _post_clean(client, scenarios["clean"])
    assert r.status_code == 201
    # Request identity is echoed and stored.
    assert r.headers["x-request-id"] == "req-it-001"
    body = r.json()
    assert body["request_id"] == "req-it-001"
    assert body["status"] == "done"
    assert body["pipeline_version"].startswith("driftcorr-pipeline/")

    rep = body["result"]
    assert rep["status"] == "ok"
    assert rep["estimate"]["offset_s"] == pytest.approx(TRUE_OFFSET_S, abs=2e-3)
    assert rep["estimate"]["drift_ppm"] == pytest.approx(TRUE_DRIFT_PPM, abs=5.0)
    assert rep["alignment_check"]["residual_rms_ms"] < 2.0

    job_id = body["job_id"]
    r2 = client.get(f"/jobs/{job_id}")
    assert r2.status_code == 200
    assert r2.json()["result"]["estimate"]["drift_ppm"] == pytest.approx(
        TRUE_DRIFT_PPM, abs=5.0)

    r3 = client.get("/jobs")
    assert any(j["job_id"] == job_id for j in r3.json())


def test_corrupted_job_flags_outliers(client, scenarios):
    r = _post_clean(client, scenarios["corrupted"], request_id="req-it-002")
    assert r.status_code == 201
    est = r.json()["result"]["estimate"]
    outliers = [p["ref_time_s"] for p in est["sync_points"] if not p["inlier"]]
    assert sorted(outliers) == [3.5, 7.5]


def test_degenerate_job_fails_with_classified_reason(client, scenarios):
    r = _post_clean(client, scenarios["degenerate"], request_id="req-it-003")
    assert r.status_code == 201  # job accepted; the *pipeline* failed
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_class"] == "insufficient_evidence"
    assert body["error_message"]
    # Failure reason is also visible inside the stored report.
    assert body["result"]["failure"]["error_class"] == "insufficient_evidence"


def test_missing_input_file_is_a_400(client, scenarios):
    r = client.post("/jobs", json={
        "reference_path": "/nonexistent/ref.wav",
        "target_path": scenarios["clean"]["target_wav"],
        "reference_metadata_path": scenarios["clean"]["reference_meta"],
    })
    assert r.status_code == 400
    assert r.json()["error_class"] == "invalid_input"


def test_unknown_job_is_a_404(client):
    r = client.get("/jobs/doesnotexist")
    assert r.status_code == 404
    assert r.json()["error_class"] == "job_not_found"


def test_generated_request_id_when_header_absent(client, scenarios):
    r = client.post("/jobs", json={
        "reference_path": scenarios["clean"]["reference_wav"],
        "target_path": scenarios["clean"]["target_wav"],
        "reference_metadata_path": scenarios["clean"]["reference_meta"],
    })
    assert r.status_code == 201
    rid = r.headers["x-request-id"]
    assert rid and r.json()["request_id"] == rid
