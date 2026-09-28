"""Job-store tests: status transitions, error classification, event trail."""

import logging

import pytest

from fixtures import expected
from mp4timeline.jobs import JobStore, run_parse_job

from .conftest import fixture_path

log = logging.getLogger("mp4timeline.tests")


@pytest.fixture()
def store(tmp_path):
    s = JobStore(str(tmp_path / "jobs.db"))
    yield s
    s.close()


def test_successful_job_records_steps_and_result(store, run_identity):
    job_id = store.create(fixture_path("bframes.mp4"))
    run_parse_job(store, job_id, fixture_path("bframes.mp4"), 1 << 20)
    job = store.get(job_id)
    log.info("RUN=%s job=%s status=%s", run_identity, job_id, job["status"])
    assert job["status"] == "done"
    assert job["input_sha256"] and job["input_size"] > 0

    events = [e["event"] for e in store.events(job_id)]
    log.info("RUN=%s job=%s events=%s", run_identity, job_id, events)
    assert events[0] == "created" and events[-1] == "done"
    assert "boxes_parsed" in events and "track_timeline_built" in events

    result = store.result(job_id)
    assert result["source"]["sha256"] == job["input_sha256"]
    assert result["tracks"][0]["samples"][0]["pts"] == 6000


@pytest.mark.parametrize("name", sorted(expected.BAD_FIXTURES))
def test_failed_job_keeps_category_not_success(store, name, run_identity):
    category = expected.BAD_FIXTURES[name]
    job_id = store.create(fixture_path(name))
    run_parse_job(store, job_id, fixture_path(name), 1 << 20)
    job = store.get(job_id)
    log.info("RUN=%s job=%s case=%s status=%s category=%s",
             run_identity, job_id, name, job["status"], job["error_category"])
    assert job["status"] == "failed"
    assert job["error_category"] == category
    assert job["error_message"]
    assert store.result(job_id) is None
    events = [e["event"] for e in store.events(job_id)]
    assert events[-1] == "failed"


def test_missing_input_file_fails_as_input_error(store):
    job_id = store.create("/nonexistent/nope.mp4")
    run_parse_job(store, job_id, "/nonexistent/nope.mp4", 1 << 20)
    job = store.get(job_id)
    assert job["status"] == "failed"
    assert job["error_category"] == "input_error"


def test_oversize_input_rejected_before_parse(store, tmp_path):
    big = tmp_path / "big.mp4"
    big.write_bytes(b"\x00" * 1024)
    job_id = store.create(str(big))
    run_parse_job(store, job_id, str(big), max_file_bytes=16)
    job = store.get(job_id)
    assert job["status"] == "failed"
    assert job["error_category"] == "input_error"


def test_unknown_job_returns_none(store):
    assert store.get("doesnotexist") is None
    assert store.result("doesnotexist") is None
