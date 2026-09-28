"""作业状态机、资源耗尽、事件环形截断、块持久化。"""
from __future__ import annotations

import pytest

from app.errors import ResourceExhaustedError, StateConflictError
from app.store import STATUS_FINALIZED, STATUS_OPEN, JobStore


@pytest.fixture()
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db", event_ring=5)
    yield s
    s.close()


def _create(store, n=1000):
    return store.create_job({"a": 1}, {"container": "pcm",
                                        "sample_format": "s16",
                                        "sample_rate": 1000,
                                        "channels": 1},
                            1000, 1, n)


def test_lifecycle_and_state_conflict(store):
    jid = _create(store)
    assert store.get_job(jid)["status"] == STATUS_OPEN
    store.add_chunk(jid, b"\x00" * 4, frames=2)
    store.mark_finalized(jid, [{"start": 0, "end": 2}], 2, 1000, 1)
    with pytest.raises(StateConflictError):
        store.require_state(jid, STATUS_OPEN)
    # finalized 后再写块 -> 状态冲突
    with pytest.raises(StateConflictError):
        store.add_chunk(jid, b"\x00" * 2, frames=1)


def test_max_jobs_resource_exhausted(store):
    for _ in range(3):
        _create(store, n=3)
    with pytest.raises(ResourceExhaustedError) as ei:
        _create(store, n=3)
    assert ei.value.details["max_jobs"] == 3


def test_chunks_persisted_in_order(store):
    jid = _create(store)
    store.add_chunk(jid, b"aaaa", 2)
    store.add_chunk(jid, b"bbbbbb", 3)
    chunks = list(store.iter_chunks(jid))
    assert [(seq, blob) for seq, _f, blob in chunks] == \
        [(0, b"aaaa"), (1, b"bbbbbb")]


def test_event_ring_truncation(store):
    jid = _create(store)
    for k in range(12):
        store.add_event(jid, f"run-{k}", "t", {"k": k})
    events = store.list_events(jid, limit=100)
    assert len(events) == 5  # 仅保留最近 5 条
    assert [e["k"] for e in events] == [7, 8, 9, 10, 11]
    # run_id 随事件保留，可据此重放
    assert events[0]["run_id"] == "run-7"


def test_mark_failed_records_error(store):
    jid = _create(store)
    store.mark_failed(jid, {"code": "COMPUTATION_FAILED",
                            "message": "boom"})
    row = store.get_job(jid)
    assert row["status"] == "failed"
    assert "boom" in row["error_json"]
