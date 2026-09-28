"""Tests for SQLite job persistence and the threaded job manager."""
from __future__ import annotations

import time

import pytest

from app.jobs.store import STATUS_DONE, STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING, JobStore
from tests.fixtures import ts_builder as tb


def test_job_lifecycle_done(manager, store):
    data = tb.SCENARIOS["clean"]().data
    job_id = manager.submit(data, input_name="clean.ts")
    # The worker pool may have picked the job up immediately, so queued and
    # running are both valid post-submit states; a terminal state is not.
    assert store.get_job(job_id)["status"] in {STATUS_QUEUED, STATUS_RUNNING}
    assert manager.wait(job_id, timeout=10.0) is True
    row = store.get_job(job_id)
    assert row["status"] == STATUS_DONE
    assert row["input_size"] == len(data)
    assert len(row["input_sha256"]) == 64
    assert row["error"] is None

    report = store.get_report(job_id)
    assert report is not None
    assert report["record_id"] == job_id
    assert report["framing"]["packets_parsed"] == len(data) // 188
    # The report carries record id on every diagnostic.
    for event in report["diagnostics"]["events"]:
        assert event["record_id"] == job_id

    events = store.get_events(job_id, limit=1000)
    codes = {e["code"] for e in events}
    assert "sync_locked" in codes
    assert "pat_version_switch" in codes
    # Pagination
    assert store.get_events(job_id, limit=1, offset=1)[0]["seq"] == 1


def test_failed_analysis_job_records_error(manager, store):
    garbage = bytes(i % 256 if i % 256 != 0x47 else 0 for i in range(500))
    job_id = manager.submit(garbage, input_name="garbage.bin")
    manager.wait(job_id, timeout=10.0)
    row = store.get_job(job_id)
    # Garbage-only input is a handled terminal state: job completes and the
    # report carries a fatal framing status (not an unhandled exception).
    assert row["status"] == STATUS_DONE
    report = store.get_report(job_id)
    assert report["framing"]["fatal"] is not None
    assert any(
        e["code"] == "sync_recovery_failed"
        for e in store.get_events(job_id, limit=1000)
    )


def test_report_persistence_roundtrip_after_reopen(tmp_path):
    from app.jobs.manager import JobManager

    db_path = str(tmp_path / "persist.db")
    store = JobStore(db_path)
    manager = JobManager(store, __import__("app.config", fromlist=["Settings"]).Settings(
        db_path=db_path
    ))
    data = tb.SCENARIOS["clean"]().data
    job_id = manager.submit(data, input_name="x.ts")
    manager.wait(job_id, timeout=10.0)
    manager.shutdown()
    store.close()

    reopened = JobStore(db_path)
    row = reopened.get_job(job_id)
    assert row["status"] == STATUS_DONE
    assert reopened.get_report(job_id)["record_id"] == job_id
    assert len(reopened.get_events(job_id, limit=1000)) > 0
    reopened.close()


def test_unknown_job_is_absent(store):
    assert store.get_report("does-not-exist") is None
    assert store.get_job("does-not-exist") is None
