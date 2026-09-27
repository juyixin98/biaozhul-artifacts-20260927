"""HTTP contract: correlation ids, error semantics, persisted runs."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("JB_DB_PATH", tmp.name)
    # import after env so the module-level default is overridden
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    os.unlink(tmp.name)


def test_health_reports_version_and_request_id(client):
    r = client.get("/health", headers={"x-request-id": "rid-abc"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["request_id"] == "rid-abc"
    assert body["version"]


def test_unknown_fixture_is_404_with_code(client):
    r = client.post("/api/analyze", json={"fixture": "nope"})
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "UNKNOWN_FIXTURE"
    assert "available" in body


def test_analyze_burst_fixture_end_to_end(client):
    r = client.post("/api/analyze", json={"fixture": "burst_reorder"},
                    headers={"x-request-id": "rid-burst"})
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == "rid-burst"
    job_id = body["job_id"]
    v = body["verdict"]
    assert v["comparison"]["both_monotonic"] is True
    assert v["adaptive"]["drop_categories"]["DUPLICATE"] == 1
    # failure categories are explicitly glossed
    assert "LATE_AFTER_PLAYOUT" in v["failure_category_glossary"]

    # the job persisted and its events carry the correlation id
    detail = client.get(f"/api/jobs/{job_id}").json()["job"]
    assert detail["status"] == "COMPLETED"
    assert detail["request_id"] == "rid-burst"
    kinds = [e["event"] for e in detail["events"]]
    assert kinds[0] == "JOB_CREATED"
    assert "JOB_COMPLETED" in kinds

    # both runs are retrievable
    for mode in ("adaptive", "fixed"):
        run = client.get(f"/api/jobs/{job_id}/runs/{mode}").json()["run"]
        assert run["mode"] == mode
        assert run["monotonic"] is True


def test_job_listing_and_missing_job(client):
    client.post("/api/analyze", json={"fixture": "wraparound"})
    jobs = client.get("/api/jobs").json()["jobs"]
    assert len(jobs) == 1
    r = client.get("/api/jobs/deadbeef")
    assert r.status_code == 404
    assert r.json()["error"] == "JOB_NOT_FOUND"


def test_uploaded_trace_with_gap(client):
    packets = [
        {"seq": 0, "timestamp": 0, "ssrc": 7, "arrival_ms": 0,
         "payload_hex": "808080"},
        # seq 1 missing
        {"seq": 2, "timestamp": 160, "ssrc": 7, "arrival_ms": 21},
    ]
    r = client.post("/api/analyze/trace", json={"packets": packets})
    assert r.status_code == 200
    v = r.json()["verdict"]
    assert v["adaptive"]["gap_items"] == 1


def test_trace_rejects_unknown_config_keys(client):
    packets = [{"seq": 0, "timestamp": 0, "ssrc": 1, "arrival_ms": 0}]
    r = client.post("/api/analyze/trace",
                    json={"packets": packets, "config": {"bogus": 1}})
    assert r.status_code == 422
    assert r.json()["error"] == "UNKNOWN_CONFIG_KEYS"


def test_config_bounds_override_take_effect(client):
    # tiny ceiling forces the adaptive delay to clip aggressively
    packets = [{"seq": i, "timestamp": i * 80, "ssrc": 3,
                "arrival_ms": i * 10 + (40 if i % 4 == 0 else 0)}
               for i in range(60)]
    r = client.post("/api/analyze/trace", json={
        "packets": packets,
        "config": {"min_delay_ms": 5, "max_delay_ms": 25,
                   "jitter_multiplier": 8}})
    v = r.json()["verdict"]
    assert v["adaptive"]["delay_ms"]["max"] <= 25 + 1e-9
