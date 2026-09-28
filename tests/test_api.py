"""End-to-end HTTP tests exercising the real FastAPI app via httpx."""
from __future__ import annotations

import base64
import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from resamp.api.app import create_app
from resamp.config import Settings
from resamp.media import write_wav


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "db.sqlite"),
        data_dir=str(tmp_path / "jobs"),
        log_dir=str(tmp_path / "logs"),
        max_input_chunk_samples=20000,
        max_total_input_samples=200000,
        max_filter_taps=2_000_001,
        json_list_sample_cap=256,
        min_rate=1, max_rate=10_000_000, max_ratio_factor=4096,
        attenuation_db=80.0, transition_half_width=0.1,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
    app.state.service.close()


def _b64(x: np.ndarray) -> str:
    return base64.b64encode(np.asarray(x, dtype="<f8").tobytes()).decode()


def _decode(resp_payload) -> np.ndarray:
    raw = base64.b64decode(resp_payload["data"])
    return np.frombuffer(raw, dtype="<f8").copy()


def test_health_and_request_id(client):
    r = client.get("/health", headers={"x-request-id": "fixed-id"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "fixed-id"


def test_design_endpoint_reports_cutoffs_delay_padding(client):
    r = client.post("/v1/design", json={"fin": 8000, "fout": 48000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["l"] == 6 and body["m"] == 1
    assert body["cutoff_hz"] == 4000.0
    assert body["fpass_hz"] == 3600.0
    assert body["fstop_hz"] == 4400.0
    assert body["head_pad_input_samples"] == body["half_taps"] // 6
    assert body["tail_pad_input_samples"] == 2 * body["half_taps"] // 6 + 2
    assert body["group_delay_seconds"] == pytest.approx(
        body["half_taps"] / body["high_rate"])
    assert body["output_count_example"]["n_out"] == [0, 152, 6146]


def test_design_rejects_extreme_ratio(client):
    r = client.post("/v1/design", json={"fin": 8000, "fout": 8001})
    assert r.status_code == 413
    assert r.json()["category"] == "resource_exhausted"


def test_streaming_job_full_flow(client):
    r = client.post("/v1/jobs", json={"fin": 8000, "fout": 16000})
    assert r.status_code == 201
    jid = r.json()["job"]["job_id"]

    x = np.sin(2 * np.pi * 440 * np.arange(8000) / 8000)
    n_out = 0
    for i in range(0, x.size, 137):
        chunk = x[i:i + 137]
        rr = client.post(f"/v1/jobs/{jid}/push",
                         json={"encoding": "base64-float64-le",
                               "data": _b64(chunk)})
        assert rr.status_code == 200, rr.text
        n_out += rr.json()["n_output"]
    rf = client.post(f"/v1/jobs/{jid}/flush")
    assert rf.status_code == 200
    n_out += rf.json()["n_output"]

    status = client.get(f"/v1/jobs/{jid}").json()["job"]
    assert status["state"] == "completed"
    assert status["total_in"] == 8000
    assert status["total_out"] == n_out

    result = client.get(f"/v1/jobs/{jid}/result")
    assert result.status_code == 200
    y = _decode(result.json()["output"])
    assert y.size == n_out

    chunks = client.get(f"/v1/jobs/{jid}/chunks").json()["chunks"]
    assert chunks[-1]["kind"] == "flush"


def test_push_unknown_job_404(client):
    r = client.post("/v1/jobs/nope/push",
                    json={"encoding": "json", "data": [0.1, 0.2]})
    assert r.status_code == 404
    body = r.json()
    assert body["category"] == "invalid_input"
    assert body["error"] == "not_found"
    assert "request_id" in body


def test_double_flush_409(client):
    jid = client.post("/v1/jobs", json={"fin": 8000, "fout": 16000}).json()[
        "job"]["job_id"]
    client.post(f"/v1/jobs/{jid}/push",
                json={"encoding": "json", "data": [0.1] * 10})
    assert client.post(f"/v1/jobs/{jid}/flush").status_code == 200
    r = client.post(f"/v1/jobs/{jid}/flush")
    assert r.status_code == 409
    assert r.json()["category"] == "state_conflict"


def test_nonfinite_json_payload_400(client):
    jid = client.post("/v1/jobs", json={"fin": 8000, "fout": 16000}).json()[
        "job"]["job_id"]
    # NaN/Inf are not JSON-legal, so they arrive via the binary float64 path.
    raw = np.array([0.1, np.nan, 0.3], dtype="<f8").tobytes()
    r = client.post(f"/v1/jobs/{jid}/push",
                    json={"encoding": "base64-float64-le",
                          "data": base64.b64encode(raw).decode()})
    assert r.status_code == 400
    body = r.json()
    assert body["category"] == "invalid_input"
    assert body["details"]["index"] == 1


def test_schema_error_is_invalid_input(client):
    r = client.post("/v1/jobs", json={"fin": -8000, "fout": 16000})
    assert r.status_code == 400
    assert r.json()["category"] == "invalid_input"


def test_validate_payload_endpoint(client):
    x = np.linspace(-0.5, 0.5, 200)
    r = client.post("/v1/validate/payload",
                    json={"fin": 8000,
                          "payload": {"encoding": "base64-float64-le",
                                      "data": _b64(x)}})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] and body["n_samples"] == 200
    assert body["min"] == pytest.approx(-0.5)
    assert body["duration_seconds"] == pytest.approx(200 / 8000)


def test_resample_wav_one_shot(client):
    rate_in = 8000
    x = 0.6 * np.sin(2 * np.pi * 600 * np.arange(rate_in) / rate_in)
    wav = write_wav(x, rate_in, sample_width=2)
    r = client.post("/v1/resample/wav",
                    json={"wav_base64": base64.b64encode(wav).decode(),
                          "fout": 16000, "output_sample_width": 2,
                          "chunk_size": 123})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["input"]["sample_rate"] == 8000
    assert body["output"]["sample_rate"] == 16000
    out_wav = base64.b64decode(body["output"]["data"])
    with wave.open(io.BytesIO(out_wav)) as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getnframes() == body["output"]["n_samples"]


def test_resample_samples_matches_reference(client):
    from resamp.dsp.reference import resample_offline
    fin, fout = 16000, 48000
    x = np.sin(2 * np.pi * 2000 * np.arange(16000) / fin)
    r = client.post("/v1/resample/samples",
                    json={"fin": fin, "fout": fout,
                          "payload": {"encoding": "base64-float64-le",
                                      "data": _b64(x)},
                          "chunk_size": 257})
    assert r.status_code == 200
    y = _decode(r.json()["output"])
    ref, _ = resample_offline(x, fin, fout)
    assert y.shape == ref.shape
    assert np.max(np.abs(y - ref)) < 1e-9
