"""Job store: real persisted state machine, readable from a new connection."""

from __future__ import annotations

from app.jobs import (
    JOB_FAILED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_SUCCEEDED,
    JobStore,
)


def test_job_lifecycle_persisted(tmp_path):
    db = str(tmp_path / "jobs.db")
    store = JobStore(db, worker_id="worker-test")
    job_id = store.create(request_id="req-1", input_kind="wav",
                          include_blocks=True, label="lab")

    assert store.get(job_id)["status"] == JOB_PENDING
    store.mark_processing(job_id)
    assert store.get(job_id)["status"] == JOB_PROCESSING

    store.mark_succeeded(job_id, {"status": "OK", "integrated_loudness": -9.0})
    row = store.get(job_id)
    assert row["status"] == JOB_SUCCEEDED
    assert row["result"]["status"] == "OK"
    assert row["request_id"] == "req-1"
    assert row["worker_id"] == "worker-test"

    # A fresh store (simulating restart) sees the same committed state.
    reopened = JobStore(db, worker_id="worker-test")
    assert reopened.get(job_id)["status"] == JOB_SUCCEEDED


def test_failed_job_records_category(tmp_path):
    store = JobStore(str(tmp_path / "j.db"), "w")
    job_id = store.create(request_id="req-2", input_kind="wav",
                          include_blocks=False, label=None)
    store.mark_failed(job_id, "WAV_COMPRESSED_UNSUPPORTED", "mp3 not supported")
    row = store.get(job_id)
    assert row["status"] == JOB_FAILED
    assert row["failure_code"] == "WAV_COMPRESSED_UNSUPPORTED"
    assert "mp3" in row["failure_message"]


def test_listing_orders_newest_first(tmp_path):
    store = JobStore(str(tmp_path / "j.db"), "w")
    ids = [store.create(request_id=f"r{i}", input_kind="wav",
                        include_blocks=False, label=None) for i in range(3)]
    rows = store.list_recent()
    assert [r["id"] for r in rows] == list(reversed(ids))


def test_missing_job_returns_none(tmp_path):
    store = JobStore(str(tmp_path / "j.db"), "w")
    assert store.get("deadbeef") is None
