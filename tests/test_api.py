"""End-to-end HTTP tests via FastAPI's TestClient.

These exercise the real routes, JSON envelopes, request-id correlation, the
chunked job state machine, and concrete failure categories returned over the
wire.
"""
from __future__ import annotations

import numpy as np
import pytest

from tests import fixtures as fx


def _raw_pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def test_health_reports_version_and_identity(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["kernel_version"]
    assert "BS.1770" in body["specification"]
    assert body["request_id"]
    assert r.headers["X-Request-ID"] == body["request_id"]


def test_request_id_is_echoed_and_correlated(client):
    rid = "trace-abc-123"
    wav = fx.to_wav(fx.digital_silence(1.0))
    r = client.post("/analyze/wav", content=wav,
                    headers={"X-Request-ID": rid})
    assert r.json()["request_id"] == rid
    assert r.headers["X-Request-ID"] == rid


def test_analyze_wav_tone_specific_result(client):
    wav = fx.to_wav(fx.calibrated_loudness_tone(3.0, -23.0))
    body = client.post("/analyze/wav", content=wav).json()
    res = body["result"]
    assert res["status"] == "OK"
    assert res["integrated_loudness_lufs"] == pytest.approx(-23.0, abs=0.1)
    assert res["integrated_gating"]["absolute_gate_lufs"] == -70.0
    assert res["method"]["lra_overlap"].startswith("2/3")
    assert res["signal"]["sample_rate_hz"] == 48000
    assert res["true_peak"] == "NOT_MEASURED"


def test_analyze_wav_silence_category_over_http(client):
    body = client.post("/analyze/wav",
                       content=fx.to_wav(fx.digital_silence(1.0))).json()
    assert body["result"]["status"] == "SILENCE"
    assert body["result"]["integrated_loudness_lufs"] is None


def test_non_wav_payload_returns_invalid_media_code(client):
    r = client.post("/analyze/wav", content=b"not a wav")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_MEDIA"
    assert err["message"]


def test_44100_wav_returns_specific_error_code(client):
    r = client.post("/analyze/wav",
                    content=fx.to_wav(np.zeros((100, 1)), sample_rate=44100))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNSUPPORTED_SAMPLE_RATE"


def test_compressed_audio_returns_unsupported_format(client):
    import struct
    fmt_chunk = struct.pack("<HHIIHH", 0x0055, 1, 48000, 48000 * 2, 2, 16)
    riff = b"RIFF" + struct.pack("<I", 0) + b"WAVE"
    riff += b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
    riff += b"data" + struct.pack("<I", 0)
    r = client.post("/analyze/wav", content=riff)
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "UNSUPPORTED_FORMAT"


def test_chunked_job_lifecycle_matches_whole(client):
    x = fx.calibrated_loudness_tone(6.37, -23.0, channels=2)
    pcm = _raw_pcm(x)

    create = client.post("/jobs", json={
        "channels": 2, "sample_format": "s16",
    })
    assert create.status_code == 201
    job_id = create.json()["job_id"]
    assert create.json()["roles"] == ["L", "R"]

    # send in awkward byte chunks that straddle 4-byte stereo frames
    bounds = list(range(0, len(pcm), 1357)) + [len(pcm)]
    for a, b in zip(bounds, bounds[1:]):
        rr = client.post(f"/jobs/{job_id}/chunks", content=pcm[a:b])
        assert rr.status_code == 200
        assert rr.json()["state"] == "OPEN"

    fin = client.post(f"/jobs/{job_id}/finalize").json()
    res = fin["result"]
    assert res["status"] == "OK"
    assert res["integrated_loudness_lufs"] == pytest.approx(-20.0, abs=0.15)

    # result is persisted and retrievable
    got = client.get(f"/jobs/{job_id}").json()
    assert got["state"] == "FINALIZED"
    assert got["result"]["integrated_loudness_lufs"] == pytest.approx(
        res["integrated_loudness_lufs"], abs=1e-12)


def test_chunked_job_rejects_post_finalize_append(client):
    client.post("/jobs", json={"channels": 1, "sample_format": "s16"})
    j = client.post("/jobs", json={"channels": 1, "sample_format": "s16"}).json()["job_id"]
    client.post(f"/jobs/{j}/finalize")
    r = client.post(f"/jobs/{j}/chunks", content=b"\x00\x00")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_ERROR"


def test_unknown_job_is_job_error(client):
    r = client.post("/jobs/deadbeef/finalize")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "JOB_ERROR"


def test_explicit_roles_validation(client):
    r = client.post("/jobs", json={
        "channels": 2, "sample_format": "s16",
        "roles": ["L", "Banana"],
    })
    assert r.status_code == 422  # pydantic validation


def test_roles_length_mismatch_rejected(client):
    # 2 channels but 3 roles -> INVALID_LAYOUT (400)
    j = client.post("/jobs", json={
        "channels": 2, "sample_format": "s16",
        "roles": ["L", "R", "C"],
    })
    # pydantic model accepts list; mismatch caught on chunk ingest/finalize
    assert j.status_code in (201, 400)
    if j.status_code == 201:
        job_id = j.json()["job_id"]
        fin = client.post(f"/jobs/{job_id}/finalize")
        assert fin.status_code == 400
        assert fin.json()["error"]["code"] == "INVALID_LAYOUT"


def test_trailing_fragment_reported(client):
    # total bytes not a whole number of frames -> warning + leftover
    j = client.post("/jobs", json={"channels": 1, "sample_format": "s16"}).json()["job_id"]
    client.post(f"/jobs/{j}/chunks", content=b"\x00" * 5)  # 2.5 samples
    body = client.post(f"/jobs/{j}/finalize").json()
    assert body["result"]["leftover_bytes_held"] == 1
    assert any("frame" in w for w in body["result"]["warnings"])


def test_job_byte_limit_returns_job_error(client):
    # shrink the limit, then exceed it in a single chunk
    import app.main as main
    main._jobs._max_bytes = 8
    try:
        j = client.post("/jobs", json={"channels": 1, "sample_format": "s16"}).json()["job_id"]
        r = client.post(f"/jobs/{j}/chunks", content=b"\x00" * 16)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "JOB_ERROR"
        # job is marked failed; further chunks are rejected as such
        r2 = client.post(f"/jobs/{j}/chunks", content=b"\x00")
        assert r2.status_code == 409
    finally:
        main._jobs._max_bytes = main.settings.max_job_bytes


def test_job_persisted_error_is_fetchable(client):
    import app.main as main
    main._jobs._max_bytes = 4
    j = client.post("/jobs", json={"channels": 1, "sample_format": "s16"}).json()["job_id"]
    client.post(f"/jobs/{j}/chunks", content=b"\x00" * 10)
    got = client.get(f"/jobs/{j}").json()
    assert got["state"] == "FAILED"
    assert got["error"]["code"] == "JOB_ERROR"
    main._jobs._max_bytes = main.settings.max_job_bytes
