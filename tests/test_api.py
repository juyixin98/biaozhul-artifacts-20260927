from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.routes import create_app
from app.config import Settings
from app.service import ValidationOptions, run_validation

FIX = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(Settings(db_path=str(tmp_path / "jobs.db"))))


def _post(client, name, fmt, **kw):
    return client.post("/v1/validations", json={
        "format": fmt,
        "content": (FIX / name).read_text(encoding="utf-8"),
        **kw,
    })


def test_clean_file_already_valid(client):
    r = _post(client, "clean.vtt", "vtt")
    assert r.status_code == 201
    body = r.json()
    assert body["diagnostics"] == []
    assert body["repair"]["status"] == "ALREADY_VALID"


def test_chained_overlap_solved(client, rlog):
    r = _post(client, "chained_overlap.srt", "srt")
    assert r.status_code == 201
    body = r.json()
    assert [d["code"] for d in body["diagnostics"]].count("OVERLAP") == 2
    rep = body["repair"]
    assert rep["status"] == "SOLVED"
    assert rep["minimal_change_ms"] == 1000
    assert len(rep["proposal"]) == 3  # no cue dropped
    assert "Second cue starts before first ends." in rep["repaired_content"]
    moved = [p for p in rep["proposal"] if p["change_ms"] > 0]
    assert moved and all(p["reasons"] for p in moved)
    rlog("api_case", case="chained_overlap", job_id=body["job_id"],
         input_sha256=body["input_sha256"], expected_change=1000,
         actual_change=rep["minimal_change_ms"], verdict="matched")


def test_same_start_vtt(client):
    body = _post(client, "same_start.vtt", "vtt").json()
    ov = [d for d in body["diagnostics"] if d["code"] == "OVERLAP"]
    assert ov and ov[0]["details"]["same_start"] is True
    assert body["repair"]["status"] == "SOLVED"
    assert body["repair"]["minimal_change_ms"] == 2500


def test_unrepairable_not_reported_as_success(client, rlog):
    body = _post(client, "unrepairable.srt", "srt", media_duration_ms=3000).json()
    assert body["repair"]["status"] == "UNSOLVABLE"
    assert body["repair"]["proposal"] is None
    assert body["cue_count"] == 4  # nothing silently deleted
    assert [d["code"] for d in body["diagnostics"]].count("TOO_SHORT") == 4
    rlog("api_case", case="unrepairable", job_id=body["job_id"],
         verdict="UNSOLVABLE surfaced, not masked as success")


def test_budget_exceeded(client):
    body = _post(client, "chained_overlap.srt", "srt",
                 options={"budget_ms": 500}).json()
    rep = body["repair"]
    assert rep["status"] == "BUDGET_EXCEEDED"
    assert rep["minimal_change_ms"] == 1000
    assert rep["proposal"] is None


def test_parse_error_422_and_failed_job(client):
    r = client.post("/v1/validations", json={
        "format": "srt",
        "content": "not a counter\n00:00:01,000 --> 00:00:02,000\nhi\n",
    })
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "BAD_COUNTER"
    assert err["line_no"] == 1
    job = client.get(f"/v1/validations/{err['job_id']}").json()
    assert job["status"] == "FAILED"
    assert job["error"]["code"] == "BAD_COUNTER"


def test_unknown_job_404(client):
    r = client.get("/v1/validations/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_job_events_recorded(client):
    body = _post(client, "clean.vtt", "vtt").json()
    job = client.get(f"/v1/validations/{body['job_id']}").json()
    assert [e["status"] for e in job["events"]] == ["PENDING", "RUNNING", "DONE"]
    assert job["result"]["repair"]["status"] == "ALREADY_VALID"


def test_meta_versions(client):
    meta = client.get("/v1/meta").json()
    for key in ("app_version", "python", "numpy", "fastapi", "pydantic"):
        assert meta[key]


def test_multibyte_repair_preserves_text(rlog):
    content = (FIX / "multibyte.srt").read_text(encoding="utf-8")
    result = run_validation(content, "srt", ValidationOptions())
    rep = result["repair"]
    assert rep["status"] == "SOLVED"
    assert rep["minimal_change_ms"] == 500
    for line in ("多语言字符测试 — cafés, naïve, Ελληνικά",
                 "<i>强调</i> と emoji 🎬🔥 ونص عربي"):
        assert line in rep["repaired_content"]
    rlog("service_case", case="multibyte", input_sha256=result["input_sha256"],
         expected_change=500, actual_change=rep["minimal_change_ms"],
         verdict="text preserved, minimal repair")
