"""End-to-end HTTP integration tests against the FastAPI service.

Covers request/job identity correlation, the queued->terminal lifecycle,
artifact retrieval, the validation endpoint, and explicit failure categories
for bad media and insufficient evidence. Uses a real ASGI transport (httpx)
and a real on-disk SQLite store under a temp home -- nothing is mocked.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from fixturegen import generate  # noqa: E402

from clockalign.api import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOCKALIGN_HOME", str(tmp_path / "home"))
    from clockalign.config import load_config
    cfg = load_config()
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c, cfg
    app.state.jobs.shutdown()


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    out = tmp_path_factory.mktemp("fx")
    generate(out)
    return out


def _wait_terminal(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/v1/jobs/{job_id}")
        status = r.json()["status"]
        if status in ("succeeded", "failed",
                      "rejected_insufficient_evidence"):
            return r
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_health_reports_version_and_config(client):
    c, cfg = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["config"] == str(cfg.source)


def test_request_id_is_echoed_and_correlated(client, fixtures):
    c, _ = client
    rid = "integration-req-001"
    r = c.post("/api/v1/align",
               json={"stereo_path": str(fixtures / "drift_offset.wav")},
               headers={"X-Request-ID": rid})
    assert r.status_code == 202
    assert r.headers["x-request-id"] == rid
    job_id = r.json()["job_id"]

    final = _wait_terminal(c, job_id)
    body = final.json()
    assert body["status"] == "succeeded"
    assert body["request_id"] == rid
    # Every event is traceable through the pipeline steps.
    steps = [e["step"] for e in body["events"]]
    assert "media.loaded" in steps
    assert "fit.robust" in steps
    assert "resample.correct" in steps
    assert body["version"]
    assert body["report"]["estimate"]["drift_ppm"] == pytest.approx(120, abs=15)


def test_artifacts_separate_audio_and_timeline_map(client, fixtures):
    c, _ = client
    r = c.post("/api/v1/align",
               json={"stereo_path": str(fixtures / "drift_offset.wav")})
    job_id = r.json()["job_id"]
    _wait_terminal(c, job_id)

    wav = c.get(f"/api/v1/jobs/{job_id}/artifacts/corrected_slave.wav")
    assert wav.status_code == 200
    assert wav.headers["content-type"] == "audio/wav"

    tmap = c.get(f"/api/v1/jobs/{job_id}/artifacts/timeline_map.json")
    assert tmap.status_code == 200
    body = tmap.json()
    assert body["global_model"]["drift_ppm"] == pytest.approx(120, abs=15)
    assert "segments" in body and "gaps" in body


def test_insufficient_evidence_is_a_terminal_status_not_500(client, fixtures):
    c, _ = client
    r = c.post("/api/v1/align",
               json={"stereo_path": str(fixtures / "insufficient_sync.wav")})
    job_id = r.json()["job_id"]
    final = _wait_terminal(c, job_id)
    body = final.json()
    assert body["status"] == "rejected_insufficient_evidence"
    assert body["failure"]["code"] == "insufficient_evidence"
    # No corrected artifact is advertised.
    assert c.get(
        f"/api/v1/jobs/{job_id}/artifacts/corrected_slave.wav").status_code == 404


def test_bad_media_returns_typed_422_before_queueing(client, tmp_path):
    c, _ = client
    missing = str(tmp_path / "nope.wav")
    r = c.post("/api/v1/align", json={"stereo_path": missing})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "media_error"


def test_missing_inputs_are_400(client):
    c, _ = client
    r = c.post("/api/v1/align", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_request"


def test_validate_endpoint_against_truth(client, fixtures):
    c, _ = client
    r = c.post("/api/v1/align",
               json={"stereo_path": str(fixtures / "drift_offset.wav")})
    job_id = r.json()["job_id"]
    _wait_terminal(c, job_id)
    truth = json.loads(
        (fixtures / "drift_offset.truth.json").read_text())
    v = c.post("/api/v1/validate", json={
        "job_id": job_id,
        "truth": {"drift_ppm": truth["drift_ppm"],
                  "offset_s": truth["offset_s"],
                  "drop_times_s": truth["drop_times_s"]}})
    assert v.status_code == 200
    body = v.json()
    assert body["passed"] is True
    assert body["failure_categories"] == []


def test_validate_unknown_job_is_404(client):
    c, _ = client
    r = c.post("/api/v1/validate", json={"job_id": "ghost", "truth": None})
    assert r.status_code == 404


def test_job_listing(client, fixtures):
    c, _ = client
    for _ in range(2):
        c.post("/api/v1/align",
               json={"stereo_path": str(fixtures / "bad_sync.wav")})
    r = c.get("/api/v1/jobs")
    assert r.status_code == 200
    assert len(r.json()) >= 2
    for row in r.json():
        assert row["request_id"] and row["job_id"] and row["status"]
