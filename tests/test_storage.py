"""Unit tests for the SQLite job store and status lifecycle."""
from __future__ import annotations

from clockalign.storage import JobStore


def test_job_lifecycle_and_events(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    jid = store.create_job("req-123", {"stereo_path": "/x.wav"})
    assert jid

    job = store.get_job(jid)
    assert job["status"] == "queued"
    assert job["request_id"] == "req-123"
    assert job["events"][0]["step"] == "job.queued"
    assert job["events"][0]["detail"]["request_id"] == "req-123"

    store.set_status(jid, "running")
    store.add_event(jid, "fit.robust", "ok", {"inliers": 7})
    store.set_status(jid, "succeeded", result_path="/tmp/r.json")
    job = store.get_job(jid)
    assert job["status"] == "succeeded"
    steps = [(e["step"], e["status"]) for e in job["events"]]
    assert ("fit.robust", "ok") in steps
    # Events are strictly ordered.
    seqs = [e["seq"] for e in job["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    store.close()


def test_unknown_job_is_empty(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    assert store.get_job("does-not-exist") == {}


def test_listing_newest_first(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    ids = [store.create_job(f"r{i}", {}) for i in range(3)]
    listing = store.list_jobs()
    assert [j["job_id"] for j in listing] == list(reversed(ids))
    assert all(j["request_id"].startswith("r") for j in listing)
    store.close()
