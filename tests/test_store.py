"""SQLite job store: creation, runs, status transitions, event correlation."""
import tempfile

from app.store import JobStore


def _open():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name, JobStore(f.name)


def test_job_lifecycle_and_events():
    path, store = _open()
    job_id = store.create_job("fixture:demo", {"k": 1},
                              request_id="rid-xyz", client_ref="c1")
    store.mark_running(job_id)
    store.save_run(job_id, "adaptive", {"mode": "adaptive", "n": 3})
    store.save_run(job_id, "fixed", {"mode": "fixed", "n": 3})
    store.mark_completed(job_id, {"gaps_adaptive": 0})

    job = store.get_job(job_id)
    assert job["status"] == "COMPLETED"
    assert job["request_id"] == "rid-xyz"
    assert job["config"] == {"k": 1}
    assert job["summary"] == {"gaps_adaptive": 0}

    events = [e["event"] for e in store.get_events(job_id)]
    assert events == ["JOB_CREATED", "RUNNING", "RUN_SAVED:adaptive",
                      "RUN_SAVED:fixed", "JOB_COMPLETED"]

    assert store.get_run(job_id, "adaptive")["n"] == 3
    assert store.get_run(job_id, "fixed")["mode"] == "fixed"
    assert store.get_run(job_id, "nope") is None
    store.close()


def test_failed_job_records_error():
    path, store = _open()
    job_id = store.create_job("uploaded_trace", {}, request_id="r")
    store.mark_failed(job_id, "ValueError: bad")
    job = store.get_job(job_id)
    assert job["status"] == "FAILED"
    assert job["error"] == "ValueError: bad"
    store.close()


def test_listing_newest_first():
    path, store = _open()
    ids = [store.create_job(f"src-{i}", {}, f"r{i}") for i in range(3)]
    listed = [j["job_id"] for j in store.list_jobs()]
    assert listed == list(reversed(ids))
    store.close()
