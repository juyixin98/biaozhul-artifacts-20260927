"""End-to-end API tests via FastAPI TestClient.

Assert concrete measured values, failure categories, request correlation and
job-state transitions — not merely that endpoints respond.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from conftest import SR, sine, write_pcm_wav_bytes, write_wav_bytes


@pytest.fixture
def client(tmp_path):
    import os
    os.environ["R128_DB_PATH"] = str(tmp_path / "api-jobs.db")
    os.environ["R128_WORKER_ID"] = "test-worker"
    app = create_app()
    with TestClient(app) as c:
        yield c


def _tone_wav(dur=8.0, level=0.5, freq=1000.0):
    return write_wav_bytes(sine(level, freq, dur).astype(np.float32), SR)


def test_health_and_version_report_identity_and_support(client):
    h = client.get("/health").json()
    assert h["status"] == "ok"
    assert "ebu-r128" in h["algorithm_id"]
    assert h["worker_id"] == "test-worker"

    v = client.get("/version").json()
    assert v["supported"]["integrated_loudness"] is True
    assert v["supported"]["loudness_range_lra"] is True
    assert v["supported"]["true_peak_tpbs1770"] is False


def test_wav_measurement_full_envelope(client):
    resp = client.post(
        "/measurements/wav",
        files={"payload": ("tone.wav", _tone_wav(), "audio/wav")},
        data={"include_blocks": "true", "label": "constant-tone"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "request_id" in body
    result = body["result"]
    assert result["status"] == "OK"
    assert result["request_id"] == body["request_id"]
    assert result["integrated_loudness"]["integrated_lufs"] == pytest.approx(-9.0656, abs=2e-3)
    assert result["loudness_range"]["lra_lu"] == pytest.approx(0.0, abs=1e-6)
    # Intermediates needed for explainability:
    g = result["integrated_loudness"]["gate_stats"]
    assert g["total_blocks"] == g["above_absolute_gate"] == g["above_both_gates"] == 77
    assert len(result["integrated_loudness"]["block_loudness_lufs"]) == 77
    # Provenance:
    assert result["provenance"]["worker_id"] == "test-worker"
    assert "BS.1770-4" in " ".join(result["provenance"]["spec_refs"])
    # True peak must never be fabricated:
    assert result["true_peak_tpfs"] is None
    assert "TRUE_PEAK_NOT_MEASURED_NOT_CLAIMED" in result["uncertainties"]


def test_request_id_is_correlated_from_header(client):
    rid = "corr-id-12345"
    resp = client.post(
        "/measurements/wav",
        files={"payload": ("t.wav", _tone_wav(dur=1.0), "audio/wav")},
        headers={"X-Request-Id": rid})
    assert resp.json()["request_id"] == rid


def test_silence_wav_returns_silence_category(client):
    quiet = write_wav_bytes(np.zeros(SR * 6, dtype=np.float32), SR)
    resp = client.post("/measurements/wav",
                       files={"payload": ("s.wav", quiet, "audio/wav")})
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["status"] == "SILENCE"
    assert result["integrated_loudness"]["integrated_lufs"] is None


def test_bad_wav_returns_specific_failure_code(client):
    resp = client.post("/measurements/wav",
                       files={"payload": ("x.wav", b"not a wav" + b"0" * 40,
                                          "audio/wav")})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "WAV_NOT_RIFF"
    assert err["step"] == "parse_wav"


def test_compressed_audio_is_rejected(client):
    # A file tagged as MP3 cannot be decoded: our parser must refuse clearly.
    resp = client.post("/measurements/wav",
                       files={"payload": ("m.wav", b"ID3" + b"\x00" * 100,
                                          "audio/wav")})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] in ("WAV_NOT_RIFF", "WAV_TOO_SHORT")


def test_raw_pcm_endpoint_measures(client):
    x = (sine(0.5, 1000.0, 4.0) * 32767).astype("<i2").tobytes()
    resp = client.post(
        "/measurements/pcm",
        files={"payload": ("p.pcm", x, "application/octet-stream")},
        data={"sample_rate": str(SR), "channels": "1", "sample_format": "s16"})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["signal"]["source_format"] == "s16"
    assert result["integrated_loudness"]["integrated_lufs"] == pytest.approx(-9.07, abs=2e-2)


def test_raw_pcm_validation_error_category(client):
    resp = client.post(
        "/measurements/pcm",
        files={"payload": ("p.pcm", b"\x00\x00", "application/octet-stream")},
        data={"sample_rate": "11025", "channels": "1", "sample_format": "s16"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "UNSUPPORTED_SAMPLE_RATE"


def test_job_flow_pending_processing_succeeded(client):
    resp = client.post("/jobs",
                       files={"payload": ("t.wav", _tone_wav(), "audio/wav")})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # Local worker executes synchronously; result must be retrievable.
    got = client.get(f"/jobs/{job_id}").json()["job"]
    assert got["status"] == "SUCCEEDED"
    assert got["result"]["status"] == "OK"
    assert got["result"]["integrated_loudness"]["integrated_lufs"] == pytest.approx(
        -9.0656, abs=2e-3)


def test_job_failure_is_persisted_with_category(client):
    # Legal RIFF/WAVE container (>= 44 bytes) with no fmt chunk.
    bogus = b"RIFF" + (60).to_bytes(4, "little") + b"WAVE" + b"junk" + \
            (40).to_bytes(4, "little") + b"\x00" * 40
    resp = client.post("/jobs",
                       files={"payload": ("t.wav", bogus, "audio/wav")})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "WAV_MISSING_FMT"
    job_id = body["job_id"]

    got = client.get(f"/jobs/{job_id}").json()["job"]
    assert got["status"] == "FAILED"
    assert got["failure_code"] == "WAV_MISSING_FMT"


def test_unknown_job_404(client):
    resp = client.get("/jobs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"
