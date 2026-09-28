"""HTTP interface tests: real state transitions, concrete outcomes and
explicit failure categories — never 'everything returns 200'."""
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.jobs.store import STATUS_COMPLETED, STATUS_FAILED


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health_and_version_report_run_identity(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and len(health["run_id"]) == 12
    ver = client.get("/version").json()
    assert ver["versions"]["app"]
    assert ver["run_id"] == health["run_id"]
    assert ver["container_ruleset"] == "mp4-constrained/v1"


def test_happy_path_job_completes_with_direct_concat(client):
    resp = client.post("/jobs", json={
        "segments": [
            {"path": "seg_ok_a.json"},
            {"path": "seg_ok_b.json"},
        ]})
    assert resp.status_code == 201
    job = resp.json()
    assert job["status"] == STATUS_COMPLETED
    assert job["decision"] == "direct_concat"
    assert job["error_category"] is None
    plan = client.get(f"/jobs/{job['job_id']}/plan").json()
    assert plan["decision"] == "direct_concat"
    assert {t["stream_type"] for t in plan["tracks"]} == {"video", "audio"}
    # per-sample: no negative DTS anywhere
    for track in plan["tracks"]:
        assert all(s["out_dts"] >= 0 for s in track["samples"])


def test_transcode_decision_is_explicit_not_faked(client):
    resp = client.post("/jobs", json={
        "segments": [
            {"path": "seg_ok_a.json"},
            {"path": "seg_tb_mismatch.json"},
        ]})
    job = resp.json()
    assert job["status"] == STATUS_COMPLETED
    assert job["decision"] == "transcode_required"
    cats = {r["category"] for r in job["plan"]["reasons"]}
    assert "TIMEBASE_MISMATCH" in cats
    assert job["plan"]["tracks"] == []
    # no plan samples exposed as if direct concat were possible
    assert client.get(f"/jobs/{job['job_id']}/plan").status_code == 200


def test_missing_reference_job_is_failed_not_success(client):
    resp = client.post("/jobs", json={
        "segments": [{"path": "seg_dangling_ref.json"}]})
    job = resp.json()
    assert resp.status_code == 201
    assert job["status"] == STATUS_FAILED
    assert job["decision"] == "failed"
    assert job["error_category"] == "MISSING_REFERENCE"
    assert "99" in job["error_detail"]
    events = client.get(f"/jobs/{job['job_id']}/events").json()["events"]
    assert any(e["step"] == "failed" and e["level"] == "ERROR" for e in events)


def test_missing_input_file_is_client_error(client):
    resp = client.post("/jobs", json={
        "segments": [{"path": "does_not_exist.json"}]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["category"] == "INPUT_ERROR"


def test_unknown_job_404_and_no_plan_409(client):
    assert client.get("/jobs/nope").status_code == 404
    resp = client.post("/jobs", json={
        "segments": [{"path": "seg_dangling_ref.json"}]})
    job_id = resp.json()["job_id"]
    # failed job never got a plan -> 409 with structured detail
    detail = client.get(f"/jobs/{job_id}/plan")
    assert detail.status_code == 409
    assert detail.json()["detail"]["category"] == "NO_PLAN"


def test_validate_endpoint_rechecks_stored_plan(client):
    job = client.post("/jobs", json={
        "segments": [{"path": "seg_ok_a.json"}]}).json()
    result = client.post(f"/jobs/{job['job_id']}/validate").json()
    assert result["ok"] is True and result["violations"] == []


def test_events_carry_steps_and_run_identity(client):
    job = client.post("/jobs", json={
        "segments": [{"path": "seg_ok_a.json"}]}).json()
    events = client.get(f"/jobs/{job['job_id']}/events").json()
    assert events["run_id"]
    steps = [e["step"] for e in events["events"]]
    assert steps[0] == "queued"
    assert "compat" in steps and "plan" in steps and steps[-1] == "completed"


def test_non_keyframe_trim_job_with_preroll(client):
    resp = client.post("/jobs", json={
        "segments": [{
            "path": "seg_midgop.json",
            "trim_in": {"num": 2, "den": 15},
            "trim_out": {"num": 4, "den": 15},
        }]})
    job = resp.json()
    assert job["status"] == STATUS_COMPLETED, job["error_detail"]
    plan = client.get(f"/jobs/{job['job_id']}/plan").json()
    video = plan["tracks"][0]["samples"]
    assert [s["src_index"] for s in video] == [0, 1, 4, 5, 6, 7, 8]
    assert {s["src_index"] for s in video if s["role"] == "preroll"} == {0, 1, 7}
    assert all(s["out_dts"] >= 0 for s in video)
