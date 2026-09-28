"""Service + SQLite job-state tests, including status codes and audit rows."""
from __future__ import annotations

import numpy as np
import pytest

from resamp.config import Settings
from resamp.errors import (InvalidInputError, NotFoundError,
                           ResourceExhaustedError, StateConflictError)
from resamp.service import ResamplingService, decode_samples, encode_samples
from resamp.storage import JobStore


@pytest.fixture
def svc(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "db.sqlite"),
        data_dir=str(tmp_path / "jobs"),
        log_dir=str(tmp_path / "logs"),
        max_input_chunk_samples=4000,
        max_total_input_samples=200000,
        max_filter_taps=2_000_001,
        json_list_sample_cap=128,
        min_rate=1, max_rate=10_000_000, max_ratio_factor=4096,
        attenuation_db=80.0, transition_half_width=0.1,
    )
    store = JobStore(settings.db_path, settings.data_dir)
    s = ResamplingService(settings, store)
    yield s
    s.close()


def test_full_lifecycle_and_persisted_counters(svc):
    created = svc.create_job(fin=8000, fout=16000)
    jid = created["job"]["job_id"]
    assert created["job"]["state"] == "created"
    x = np.linspace(-0.4, 0.4, 3000)
    r1 = svc.push(jid, x[:1000])
    r2 = svc.push(jid, x[1000:2500])
    r3 = svc.push(jid, x[2500:])
    flushed = svc.flush(jid)
    job = svc.status(jid)["job"]
    assert job["state"] == "completed"
    assert job["total_in"] == 3000
    assert (r1["n_output"] + r2["n_output"] + r3["n_output"]
            + flushed["n_output"]) == job["total_out"]
    rows = svc.chunks(jid)["chunks"]
    assert [r["kind"] for r in rows] == ["push", "push", "push", "flush"]
    assert [r["n_in"] for r in rows[:3]] == [1000, 1500, 500]
    assert sum(r["n_out"] for r in rows) == job["total_out"]
    result = svc.result(jid)
    assert result["n_samples"] == job["total_out"]


def test_push_after_flush_is_409(svc):
    jid = svc.create_job(fin=8000, fout=48000)["job"]["job_id"]
    svc.push(jid, np.ones(50))
    svc.flush(jid)
    with pytest.raises(StateConflictError) as ei:
        svc.push(jid, np.ones(2))
    assert ei.value.http_status == 409
    assert ei.value.details["state"] == "completed"


def test_double_flush_is_409(svc):
    jid = svc.create_job(fin=8000, fout=16000)["job"]["job_id"]
    svc.push(jid, np.ones(10))
    svc.flush(jid)
    with pytest.raises(StateConflictError):
        svc.flush(jid)


def test_unknown_job_is_404(svc):
    with pytest.raises(NotFoundError) as ei:
        svc.status("deadbeef")
    assert ei.value.http_status == 404
    assert ei.value.category == "invalid_input"


def test_total_input_limit_is_413(svc):
    jid = svc.create_job(fin=8000, fout=16000)["job"]["job_id"]
    for _ in range(50):
        svc.push(jid, np.ones(4000))  # exactly at the 200000 cap: allowed
    with pytest.raises(ResourceExhaustedError) as ei:
        svc.push(jid, np.ones(1))  # 200001 exceeds cap
    assert ei.value.http_status == 413
    assert ei.value.details["projected"] == 200001


def test_nonfinite_payload_rejected_before_persistence(svc):
    jid = svc.create_job(fin=8000, fout=16000)["job"]["job_id"]
    bad = np.ones(4)
    bad[2] = np.nan
    with pytest.raises(InvalidInputError):
        svc.push(jid, bad)
    job = svc.status(jid)["job"]
    assert job["total_in"] == 0


def test_base64_roundtrip():
    x = np.linspace(-1, 1, 101)
    payload = encode_samples(x)
    assert payload["encoding"] == "base64-float64-le"
    y = decode_samples(payload, list_cap=10)  # cap applies to json only
    assert np.array_equal(x, y)


def test_json_list_cap_enforced():
    with pytest.raises(ResourceExhaustedError):
        decode_samples({"encoding": "json", "data": [0.0] * 129},
                       list_cap=128)


def test_invalid_base64_and_encoding():
    with pytest.raises(InvalidInputError):
        decode_samples({"encoding": "base64-float64-le", "data": "%%%"},
                       list_cap=10)
    with pytest.raises(InvalidInputError):
        decode_samples({"encoding": "base64-float64-le",
                        "data": "AAAAAAAA"}, list_cap=10)  # 4 bytes
    with pytest.raises(InvalidInputError):
        decode_samples({"encoding": "wav", "data": []}, list_cap=10)


def test_offline_helper_matches_core(svc):
    from resamp.dsp.reference import resample_offline
    x = np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)
    y, job = svc.run_offline(x, fin=16000, fout=48000, chunk_size=123)
    ref, _ = resample_offline(x, 16000, 48000)
    assert np.max(np.abs(y - ref)) < 1e-9
    assert job["state"] == "completed"
