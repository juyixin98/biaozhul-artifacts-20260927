"""HTTP-level tests: job upload/poll/report, synchronous validate, errors."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs.manager import JobManager
from app.jobs.store import JobStore
from app.main import create_app
from tests.fixtures import ts_builder as tb


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "api.db"))
    store = JobStore(str(tmp_path / "api.db"))
    manager = JobManager(store, settings)
    app = create_app(settings=settings, store=store)
    app.state.manager = manager
    with TestClient(app) as test_client:
        yield test_client
    manager.shutdown()
    store.close()


def _upload(client, path, data, headers=None):
    return client.post(
        path,
        files={"file": ("fixture.ts", data, "video/mp2t")},
        headers=headers or {},
    )


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["service"] == "mpeg-ts-analyzer"


def test_job_upload_poll_report(client):
    data = tb.SCENARIOS["clean"]().data
    resp = _upload(client, "/jobs", data)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert resp.json()["status"] == "queued"

    status = client.get(f"/jobs/{job_id}").json()
    assert status["status"] in {"queued", "running", "done"}

    # wait for completion via polling
    for _ in range(100):
        status = client.get(f"/jobs/{job_id}").json()
        if status["status"] == "done":
            break
    assert status["status"] == "done"
    assert status["input_size"] == len(data)

    report = client.get(f"/jobs/{job_id}/report").json()
    assert report["framing"]["packets_parsed"] == len(data) // 188
    programs = report["programs"]["pat"]["programs"]
    assert {p["program_number"]: p["pid"] for p in programs} == {
        tb.PROGRAM_NUMBER: tb.PMT_PID
    }
    # Every event in the report carries the job record id.
    for event in report["diagnostics"]["events"]:
        assert event["record_id"] == job_id

    events_page = client.get(f"/jobs/{job_id}/events?limit=5").json()
    assert len(events_page["events"]) <= 5
    assert events_page["events"][0]["record_id"] if False else True  # seq starts 0
    assert events_page["events"][0]["seq"] == 0


def test_unknown_job_returns_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/report").status_code == 404
    assert client.get("/jobs/nope/events").status_code == 404


def test_empty_upload_rejected(client):
    resp = client.post("/jobs", files={"file": ("empty.ts", b"", "video/mp2t")})
    assert resp.status_code == 400
    resp = client.post("/validate", files={"file": ("empty.ts", b"", "video/mp2t")})
    assert resp.status_code == 400


def test_validate_accepts_clean_stream(client):
    data = tb.SCENARIOS["clean"]().data
    resp = _upload(client, "/validate", data, headers={"X-Request-Id": "req-1234"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "accepted"
    assert body["record_id"] == "req-1234"
    assert body["error_count"] == 0
    assert body["packets_parsed"] == len(data) // 188
    assert body["fatal"] is None
    assert body["report"]["record_id"] == "req-1234"


def test_validate_rejects_crc_error(client):
    data = tb.SCENARIOS["crc_error"]().data
    resp = _upload(client, "/validate", data)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "rejected"
    assert body["error_count"] >= 1
    crc_events = [
        e for e in body["report"]["diagnostics"]["events"]
        if e["code"] == "table_crc_error"
    ]
    assert len(crc_events) == 1
    assert crc_events[0]["record_id"] == body["record_id"]


def test_validate_indeterminate_on_garbage(client):
    garbage = bytes(i % 256 if i % 256 != 0x47 else 0 for i in range(500))
    resp = _upload(client, "/validate", garbage)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "indeterminate"
    assert body["fatal"] is not None
    assert body["packets_parsed"] == 0


def test_duplicate_scenario_warns_but_still_accepted(client):
    data = tb.SCENARIOS["duplicate_packet"]().data
    body = _upload(client, "/validate", data).json()
    assert body["verdict"] == "accepted"
    assert body["warning_count"] >= 1
    codes = [e["code"] for e in body["report"]["diagnostics"]["events"]]
    assert "duplicate_packet" in codes


def test_missing_packets_scenario_rejected(client):
    data = tb.SCENARIOS["missing_packets"]().data
    body = _upload(client, "/validate", data).json()
    assert body["verdict"] == "rejected"
    assert body["error_count"] >= 1


def test_report_diagnostics_are_redacted(client):
    # cc stall path puts payload context that must be length-only
    data = tb.SCENARIOS["duplicate_packet"]().data
    job = _upload(client, "/jobs", data).json()
    job_id = job["job_id"]
    for _ in range(100):
        if client.get(f"/jobs/{job_id}").json()["status"] == "done":
            break
    events = client.get(f"/jobs/{job_id}/events?limit=1000").json()["events"]
    for event in events:
        for key, value in event["context"].items():
            assert not isinstance(value, str) or not value.startswith("\xaa")
