"""Failure taxonomy: each error category asserts status, code and persistence."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from resampler.api.app import app
from resampler.config import Settings
from resampler.errors import (ComputationError, InputValidationError,
                              OutputOverflowError, ResourceExhaustedError,
                              StateConflictError)
from resampler.jobs import JobManager, JobParams, JobStore
from resampler.media import decode_pcm
from resampler.observability import RunLogger
from resampler.signal import StreamingPolyphase, build_plan


def _cat(err):
    return err.category


def test_input_error_variants(settings, tmp_path, runlog):
    store = JobStore(str(tmp_path / "e.db"))
    mgr = JobManager(settings, store, RunLogger(settings.log_dir, "err-input"))

    with pytest.raises(InputValidationError) as e1:
        mgr.create_job(JobParams(input_rate=0, output_rate=48000))
    runlog.check("zero rate -> input_error", _cat(e1.value) == "input_error",
                 e1.value.detail, "parameter validation before state")

    mgr.create_job(JobParams(input_rate=8000, output_rate=8000), job_id="j1")
    with pytest.raises(InputValidationError) as e2:
        mgr.add_chunk("j1", b"\x00\x00\x01")  # misaligned s16 default? use f64
    # default input format is f64le (8-byte): 3 bytes misaligned
    runlog.check("misaligned raw bytes -> input_error + failed job",
                 _cat(e2.value) == "input_error", e2.value.detail,
                 "boundary decode failure")
    row = store.get_job("j1")
    runlog.check("job marked failed with category",
                 row.state == "failed" and row.error_category == "input_error",
                 {"state": row.state, "cat": row.error_category},
                 "failures persist for replay")

    with pytest.raises(InputValidationError) as e3:
        mgr.add_chunk("does-not-exist", b"12345678")
    runlog.check("unknown job -> input_error", _cat(e3.value) == "input_error",
                 e3.value.detail, "unknown id is a client error, not 404 semantics")
    store.close()


def test_state_conflicts(settings, tmp_path, runlog):
    store = JobStore(str(tmp_path / "s.db"))
    mgr = JobManager(settings, store, RunLogger(settings.log_dir, "err-state"))
    mgr.create_job(JobParams(input_rate=8000, output_rate=8000), job_id="s1")
    mgr.add_chunk("s1", np.ones(4, dtype="<f8").tobytes())
    mgr.flush("s1")

    with pytest.raises(StateConflictError) as e1:
        mgr.add_chunk("s1", np.ones(4, dtype="<f8").tobytes())
    runlog.check("chunk after flush -> state_conflict",
                 _cat(e1.value) == "state_conflict", e1.value.detail,
                 "illegal lifecycle transition")
    with pytest.raises(StateConflictError) as e2:
        mgr.flush("s1")
    runlog.check("double flush -> state_conflict",
                 _cat(e2.value) == "state_conflict", e2.value.detail,
                 "flushed is terminal-good")

    # flushed job: state check precedes payload decode
    with pytest.raises(StateConflictError) as e3:
        mgr.add_chunk("s1", b"abc")
    runlog.check("chunk to flushed job conflicts even with bad bytes",
                 _cat(e3.value) == "state_conflict", e3.value.detail,
                 "state machine checked before media parsing")
    store.close()


def test_resource_exhaustion(settings, tmp_path, runlog):
    small = Settings(db_path=str(tmp_path / "r.db"),
                     log_dir=settings.log_dir,
                     max_samples_per_chunk=4, max_total_samples=10,
                     max_jobs=2, max_ratio_term=1_000_000, max_filter_taps=1 << 18)
    store = JobStore(str(tmp_path / "r.db"))
    mgr = JobManager(small, store, RunLogger(settings.log_dir, "err-res"))
    mgr.create_job(JobParams(input_rate=8000, output_rate=8000), job_id="r1")
    with pytest.raises(ResourceExhaustedError) as e1:
        mgr.add_chunk("r1", np.zeros(5, dtype="<f8").tobytes())
    runlog.check("chunk cap -> resource_exhausted",
                 _cat(e1.value) == "resource_exhausted", e1.value.detail,
                 "5 samples > 4/chunk; distinct from bad input")
    row = store.get_job("r1")
    runlog.check("resource failure marks job failed",
                 row.state == "failed" and
                 row.error_category == "resource_exhausted",
                 {"state": row.state}, "terminal failed")

    mgr.create_job(JobParams(input_rate=8000, output_rate=8000), job_id="r2")
    with pytest.raises(ResourceExhaustedError) as e2:
        mgr.create_job(JobParams(input_rate=8000, output_rate=8000), job_id="r3")
    runlog.check("max_jobs cap -> resource_exhausted",
                 _cat(e2.value) == "resource_exhausted", e2.value.detail,
                 "global quota")

    # per-job total cap, in a fresh store so the global quota is irrelevant
    store2 = JobStore(str(tmp_path / "r2.db"))
    mgr2 = JobManager(small, store2, RunLogger(settings.log_dir, "err-res2"))
    mgr2.create_job(JobParams(input_rate=8000, output_rate=8000), job_id="r4")
    mgr2.add_chunk("r4", np.ones(4, dtype="<f8").tobytes())
    with pytest.raises(ResourceExhaustedError) as e3:
        mgr2.add_chunk("r4", np.ones(7, dtype="<f8").tobytes())
    runlog.check("job total cap -> resource_exhausted",
                 _cat(e3.value) == "resource_exhausted", e3.value.detail,
                 "4+7 > 10 cumulative")
    store.close(); store2.close()


def test_computation_failure_nonfinite_kernel(settings, tmp_path, runlog):
    """NaN input must never reach the kernel; if it does, it is a 500.

    The manager boundary rejects NaN payloads as input errors (f32 with NaN),
    while a direct kernel push of non-finite data raises ComputationError.
    """
    plan = build_plan(8000, 8000, settings=settings)
    eng = StreamingPolyphase(plan)
    # NaNs do not come from decode_pcm, but force them into the kernel to
    # verify the kernel's own 500-class guard.
    with pytest.raises(ComputationError) as e1:
        eng.push(np.array([1.0, float("nan"), 2.0, 3.0] + [0.0] * plan.taps_per_phase))
    runlog.check("kernel non-finite output -> computation_failure",
                 _cat(e1.value) == "computation_failure", e1.value.detail,
                 "arithmetic failures are distinguishable from bad input")

    # And the media boundary separately classifies NaN payloads as input.
    import struct
    raw = struct.pack("<dd", 1.0, float("nan"))
    with pytest.raises(InputValidationError) as e2:
        decode_pcm(raw, "f64le")
    runlog.check("NaN payload at boundary -> input_error",
                 _cat(e2.value) == "input_error", e2.value.detail,
                 "boundary validation precedes computation")


def test_output_overflow_is_distinct(settings, tmp_path, runlog):
    store = JobStore(str(tmp_path / "o.db"))
    mgr = JobManager(settings, store, RunLogger(settings.log_dir, "err-out"))
    mgr.create_job(JobParams(input_rate=8000, output_rate=8000,
                             input_format="f64le", output_format="s16le",
                             clip_policy="reject"), job_id="o1")
    # Need enough samples for the overflow window to produce an output.
    x = np.ones(200) * 5.0
    with pytest.raises(OutputOverflowError) as e1:
        mgr.add_chunk("o1", x.astype("<f8").tobytes())
    runlog.check("hard clip policy -> output_overflow",
                 _cat(e1.value) == "output_overflow", e1.value.detail,
                 "422-class, distinct category from input/compute/resource")
    row = store.get_job("o1")
    runlog.check("overflow failed the job",
                 row.state == "failed" and
                 row.error_category == "output_overflow",
                 {"state": row.state}, "no partial-success ambiguity")
    store.close()


@pytest.fixture
def client(settings, tmp_path):
    app.state.settings = settings
    app.state.store = JobStore(str(tmp_path / "http.db"))
    log = RunLogger(settings.log_dir, run_id="err-http")
    from resampler.jobs import JobManager
    app.state.logger = log
    app.state.manager = JobManager(settings, app.state.store, log)
    with TestClient(app) as c:
        yield c


def test_http_status_codes_per_category(client):
    # 400 input
    r = client.post("/jobs", json={"input_rate": 8000, "output_rate": 0})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "input_error"

    r = client.post("/jobs", json={"input_rate": 8000, "output_rate": 8000,
                                   "job_id": "hc1"})
    assert r.status_code == 201
    # 409 conflict
    client.post("/jobs/hc1/chunks", content=np.zeros(8).astype("<f8").tobytes(),
                headers={"Content-Type": "application/octet-stream"})
    client.post("/jobs/hc1/flush")
    r = client.post("/jobs/hc1/chunks", content=np.zeros(8).astype("<f8").tobytes(),
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "state_conflict"

    # 400 input error on bad bytes
    r = client.post("/jobs", json={"input_rate": 8000, "output_rate": 8000,
                                   "job_id": "hc2"})
    r = client.post("/jobs/hc2/chunks", content=b"000",
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "input_error"

    # 422 overflow on reject policy
    client.post("/jobs", json={"input_rate": 8000, "output_rate": 8000,
                               "output_format": "s16le", "clip_policy": "reject",
                               "job_id": "hc3"})
    r = client.post("/jobs/hc3/chunks",
                    content=(np.ones(200) * 9.0).astype("<f8").tobytes(),
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "output_overflow"


def test_huge_ratio_filter_413(client):
    small = Settings(db_path=app.state.settings.db_path,
                     log_dir=app.state.settings.log_dir,
                     max_samples_per_chunk=4096, max_total_samples=65536,
                     max_jobs=8, max_ratio_term=1_000_000, max_filter_taps=256)
    app.state.settings = small
    # validate endpoint respects tap cap via build_plan -> resource_exhausted
    r = client.post("/resample/validate",
                    json={"input_rate": 1, "output_rate": 1000})
    assert r.status_code == 413
    assert r.json()["error"]["category"] == "resource_exhausted"
