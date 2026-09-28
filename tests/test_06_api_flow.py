"""End-to-end HTTP behavior via FastAPI TestClient."""

from __future__ import annotations

import base64
import json
import math
import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from resampler.api.app import app
from resampler.jobs.storage import JobStore
from resampler.media import build_wav, decode_pcm


@pytest.fixture
def client(settings, tmp_path):
    app.state.settings = settings
    app.state.store = JobStore(str(tmp_path / "api.db"))
    from resampler.jobs import JobManager
    from resampler.observability import RunLogger
    log = RunLogger(settings.log_dir, run_id="api-server-test")
    app.state.logger = log
    app.state.manager = JobManager(settings, app.state.store, log)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_validate_endpoint_contract(client, runlog):
    r = client.post("/resample/validate",
                    json={"input_rate": 48000, "output_rate": 16000},
                    headers={"X-Run-Id": "validate-48k-16k"})
    assert r.status_code == 200, r.text
    body = r.json()
    runlog.check("validate ratio 1/3", (body["up"], body["down"]) == (1, 3),
                 {"up": body["up"], "down": body["down"]}, "gcd reduction")
    runlog.check("validate stopband 8 kHz",
                 abs(body["stopband_edge_hz"] - 8000) < 1e-9,
                 body, "min Nyquist")
    runlog.check("validate padding K-1",
                 body["padding"]["head_zeros_input_samples"] ==
                 body["taps_per_phase"] - 1,
                 body["padding"], "fixed head/tail zero padding stated")
    runlog.check("group delay fields present",
                 set(body["group_delay"]) ==
                 {"high_rate_samples", "input_samples", "output_samples"},
                 body["group_delay"], "auditable delay contract")
    # Run artifacts written
    run_dir = os.path.join(app.state.settings.log_dir, "runs", "validate-48k-16k")
    runlog.check("per-run event log file exists",
                 os.path.exists(os.path.join(run_dir, "events.jsonl")),
                 {"dir": run_dir}, "run_id drives replay directory")


def test_full_job_raw_chunks_matches_kernel(client, settings, runlog):
    rng = np.random.default_rng(99)
    x = rng.standard_normal(1200).astype(np.float64)

    r = client.post("/jobs", json={"input_rate": 16000, "output_rate": 48000,
                                   "input_format": "f64le", "output_format": "f64le",
                                   "job_id": "e2e-raw-1"})
    assert r.status_code == 201, r.text
    job = r.json()
    run_id = job["run_id"]
    runlog.check("job created open", job["state"] == "open", job, "lifecycle")

    # Send in deliberately awkward cuts: 1,1,1,37,...
    cuts = [0, 1, 2, 3, 40, 400, 817, 1200]
    total_in = 0
    for a, b in zip(cuts[:-1], cuts[1:]):
        payload = x[a:b].astype("<f8").tobytes()
        rr = client.post(f"/jobs/e2e-raw-1/chunks", content=payload,
                         headers={"X-Run-Id": run_id,
                                  "Content-Type": "application/octet-stream"})
        assert rr.status_code == 200, rr.text
        total_in += b - a
    fr = client.post("/jobs/e2e-raw-1/flush", headers={"X-Run-Id": run_id})
    assert fr.status_code == 200, fr.text
    fbody = fr.json()
    runlog.check("flush state", fbody["state"] == "flushed", fbody, "terminal")
    runlog.check("total inputs 1200", fbody["total_input_samples"] == 1200, fbody,
                 "counters persist across chunks")

    # Compare against the kernel directly
    from resampler.signal import StreamingPolyphase, build_plan
    plan = build_plan(16000, 48000, settings=settings)
    eng = StreamingPolyphase(plan)
    ref = np.concatenate([eng.push(x), eng.flush()])
    runlog.check("count matches kernel", fbody["total_output_samples"] == ref.size,
                 {"api": fbody["total_output_samples"], "kernel": ref.size},
                 "count contract over HTTP")

    res = client.get("/jobs/e2e-raw-1/result")
    assert res.status_code == 200
    got = np.frombuffer(res.content, dtype="<f8")
    runlog.check("result bytes decode to exact kernel output",
                 np.array_equal(got, ref), {"len": got.size},
                 "float64 canonical transport, chunking transparent")
    runlog.check("result headers carry delay",
                 float(res.headers["X-Group-Delay-Input"]) == plan.delay_input,
                 {"h": res.headers.get("X-Group-Delay-Input")},
                 "auditable alignment metadata")

    jres = client.get("/jobs/e2e-raw-1/result?format=json").json()
    dec = np.frombuffer(base64.b64decode(jres["data_b64"]), dtype="<f8")
    runlog.check("json base64 result identical", np.array_equal(dec, ref),
                 {"len": dec.size}, "raw and json transports agree")


def test_integer_output_clipping_counters(client, runlog):
    x = np.array([0.5, 1.5, -1.5, 0.25])
    r = client.post("/jobs", json={"input_rate": 8000, "output_rate": 8000,
                                   "input_format": "f64le", "output_format": "s16le",
                                   "clip_policy": "clip", "job_id": "clip-1"})
    assert r.status_code == 201
    assert client.post("/jobs/clip-1/chunks", content=x.astype("<f8").tobytes(),
                       headers={"Content-Type": "application/octet-stream"}).status_code == 200
    f = client.post("/jobs/clip-1/flush").json()
    runlog.check("2 clipped samples reported", f["clipped_samples"] == 2,
                 f, "rails counted and persisted")
    raw = client.get("/jobs/clip-1/result").content
    got = np.frombuffer(raw, dtype="<i2")
    runlog.check("s16 rails present",
                 int(np.max(got)) == 32767 and int(np.min(got)) == -32768,
                 {"max": int(np.max(got)), "min": int(np.min(got))},
                 "saturation, not modulo wrap")


def test_wav_job_full_container(client, runlog):
    x = (0.4 * np.sin(2 * np.pi * 440 * np.arange(3200) / 8000.0)).astype(np.float64)
    blob = build_wav(x, 8000, "s16le")
    r = client.post("/jobs", json={"input_rate": 8000, "output_rate": 12000,
                                   "input_container": "wav",
                                   "input_format": "s16le",
                                   "output_format": "s16le",
                                   "output_container": "wav",
                                   "job_id": "wav-1"})
    assert r.status_code == 201
    cr = client.post("/jobs/wav-1/chunks", content=blob,
                     headers={"Content-Type": "audio/wav"})
    assert cr.status_code == 200, cr.text
    fr = client.post("/jobs/wav-1/flush")
    assert fr.status_code == 200
    res = client.get("/jobs/wav-1/result")
    assert res.headers["content-type"] == "audio/wav"
    from resampler.media import parse_wav
    parsed = parse_wav(res.content)
    runlog.check("output WAV is mono at 12 kHz",
                 parsed.info.sample_rate == 12000 and parsed.info.n_frames > 0,
                 parsed.info.__dict__, "container rate updated on output")


def test_status_reports_counters(client):
    client.post("/jobs", json={"input_rate": 8000, "output_rate": 8000,
                               "job_id": "stat-1"})
    x = np.arange(10, dtype=np.float64) * 0.01
    client.post("/jobs/stat-1/chunks", content=x.astype("<f8").tobytes(),
                headers={"Content-Type": "application/octet-stream"})
    st = client.get("/jobs/stat-1").json()
    assert st["state"] == "open"
    assert st["input_samples"] == 10
    assert st["chunks_received"] == 1
    assert "plan" in st and st["plan"]["up"] == 1


def test_result_before_flush_conflict(client):
    client.post("/jobs", json={"input_rate": 8000, "output_rate": 8000,
                               "job_id": "early-1"})
    r = client.get("/jobs/early-1/result")
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "state_conflict"
