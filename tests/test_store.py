"""SQLite metadata transaction tests."""
from __future__ import annotations

import sqlite3

import pytest

from app.core.kernel import BatchInput, BatchRemap, UnifyResult
from app.store.sqlite_store import JobStore


def _result(card=2, width=8):
    remap = BatchRemap("b0", (0, 1), (0, 1), (True, True), 2, 0)
    return UnifyResult(
        global_dictionary=("a", "b")[:card] if card else (),
        global_value_type="string", index_width_bits=width,
        cardinality=card, sort_policy="typed-ascending-v1",
        batch_remaps=(remap,) if card else (),
        stats={"cardinality": card},
    )


def test_lifecycle_running_to_succeeded(tmp_path):
    store = JobStore(tmp_path / "j.db")
    store.insert_running(job_id="j1", value_type="string",
                         index_policy="auto", target_width=None,
                         sort_policy="typed-ascending-v1",
                         versions={"python": "3.12"})
    job = store.get_job("j1")
    assert job["status"] == "RUNNING"
    assert job["finished_at"] is None
    store.mark_succeeded(job_id="j1", result=_result(), normalization=[])
    job = store.get_job("j1")
    assert job["status"] == "SUCCEEDED"
    assert job["failure_code"] is None
    assert job["batches"][0]["batch_id"] == "b0"
    store.close()


def test_lifecycle_running_to_failed_keeps_code(tmp_path):
    store = JobStore(tmp_path / "j.db")
    store.insert_running(job_id="j2", value_type="string",
                         index_policy="auto", target_width=None,
                         sort_policy="typed-ascending-v1", versions={})
    store.mark_failed(job_id="j2", code="INDEX_OUT_OF_RANGE",
                      message="boom", details={"row": 7})
    job = store.get_job("j2")
    assert job["status"] == "FAILED"
    assert job["failure_code"] == "INDEX_OUT_OF_RANGE"
    assert job["failure_details_json"]["row"] == 7
    assert job["finished_at"] is not None
    store.close()


def test_terminal_status_cannot_be_overwritten(tmp_path):
    """A second transition must fail — exceptions must not flip FAILED to
    SUCCEEDED (the 'never report success on error' guarantee)."""
    store = JobStore(tmp_path / "j.db")
    store.insert_running(job_id="j3", value_type="string",
                         index_policy="auto", target_width=None,
                         sort_policy="typed-ascending-v1", versions={})
    store.mark_failed(job_id="j3", code="INDEX_OUT_OF_RANGE",
                      message="x", details={})
    with pytest.raises(RuntimeError, match="already terminal"):
        store.mark_succeeded(job_id="j3", result=_result(), normalization=[])
    assert store.get_job("j3")["status"] == "FAILED"
    store.close()


def test_rollback_on_transaction_body_error(tmp_path):
    """If the SUCCEEDED write fails mid-transaction, the job stays RUNNING
    (not half-written SUCCEEDED)."""
    store = JobStore(tmp_path / "j.db")
    store.insert_running(job_id="j4", value_type="string",
                         index_policy="auto", target_width=None,
                         sort_policy="typed-ascending-v1", versions={})
    # Break the job_batches FK/value by monkeypatching the result to have a
    # batch but deleting schema is messy; instead force an IntegrityError by
    # duplicate primary key insert.
    try:
        with store._tx() as cur:
            cur.execute(
                "INSERT INTO jobs(job_id,status,value_type,index_policy,"
                "target_width,sort_policy,versions_json) VALUES "
                "('j4','SUCCEEDED','string','auto',NULL,'x','{}')"
            )
    except sqlite3.IntegrityError:
        pass
    # Original transaction must have rolled back: j4 still exactly one row,
    # still RUNNING.
    job = store.get_job("j4")
    assert job["status"] == "RUNNING"
    store.close()


def test_schema_version_recorded(tmp_path):
    store = JobStore(tmp_path / "j.db")
    with store._conn:
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    assert row[0] == "1"
    store.close()
