"""HTTP 端到端：作业生命周期、跨块、错误码分类、run_id、重放事件。"""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.media import wav_bytes
from app.web import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path), max_samples_per_job=10_000,
        max_chunk_bytes=4096, max_jobs=10, event_ring=500)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


CONFIG = {
    "enter_threshold": 0.03, "exit_threshold": 0.08,
    "min_silence_ms": 100, "min_speech_ms": 50,
    "pad_before_ms": 10, "pad_after_ms": 20,
}


def make_pcm(x: np.ndarray) -> bytes:
    return (np.asarray(x, dtype=np.float32)).tobytes()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_pcm_streaming_cross_chunk_lifecycle(client):
    # sr=1000: 语音 200 / 静音 400 / 语音 200 / 静音 400，共 1200
    x = np.zeros(1200, dtype=np.float32)
    x[0:200] = 0.5
    x[600:800] = 0.5

    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    assert r.status_code == 201, r.text
    jid = r.json()["job_id"]
    assert r.headers["X-Run-ID"].startswith("run-")

    # 切成非对齐语义块（37 与 run 边界、阈值都无关）
    for a in range(0, 1200, 37):
        rr = client.post(f"/jobs/{jid}/chunks",
                         content=make_pcm(x[a:min(a + 37, 1200)]))
        assert rr.status_code == 200, rr.text

    fr = client.post(f"/jobs/{jid}/finalize")
    assert fr.status_code == 200
    body = fr.json()
    assert body["status"] == "finalized"
    assert body["total_samples"] == 1200
    assert [[iv["start"], iv["end"]]
            for iv in body["intervals_committed"]] == \
        [[0, 220], [590, 820]]
    assert body["stats"] == {"num_intervals": 2, "kept_samples": 220 + 230,
                             "dropped_samples": 1200 - 450}
    # ms 字段
    iv0 = body["intervals_committed"][0]
    assert iv0["start_ms"] == 0.0
    assert iv0["end_ms"] == 220.0


def test_finalize_query_param_single_request(client):
    x = np.zeros(300, dtype=np.float32)
    x[0:100] = 0.5
    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    jid = r.json()["job_id"]
    rr = client.post(f"/jobs/{jid}/chunks?finalize=true",
                     content=make_pcm(x))
    assert rr.json()["status"] == "finalized"
    assert [[iv["start"], iv["end"]]
            for iv in rr.json()["intervals_committed"]] == [[0, 120]]


def test_all_silence_job_empty_intervals(client):
    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "s16",
                  "sample_rate": 1000, "channels": 1}})
    jid = r.json()["job_id"]
    client.post(f"/jobs/{jid}/chunks", content=b"\x00" * 400)  # 200 帧
    body = client.post(f"/jobs/{jid}/finalize").json()
    assert body["intervals_committed"] == []
    assert body["stats"]["kept_samples"] == 0
    assert body["stats"]["dropped_samples"] == 200


def test_wav_one_shot_and_segment_endpoint(client):
    x = np.zeros(1000)
    x[100:300] = 0.5
    blob = wav_bytes(x, 1000)
    r = client.post(
        "/segment?enter_threshold=0.03&exit_threshold=0.08"
        "&min_silence_ms=100&min_speech_ms=50"
        "&pad_before_ms=10&pad_after_ms=20",
        content=blob)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_rate"] == 1000
    assert [[iv["start"], iv["end"]]
            for iv in body["intervals_committed"]] == [[90, 320]]


def test_db_threshold_input(client):
    # -30.46dB ~= 0.03, -21.94dB ~= 0.08
    r = client.post("/jobs", json={
        "config": {"enter_threshold_db": -30.46,
                   "exit_threshold_db": -21.94,
                   "min_silence_ms": 100, "min_speech_ms": 50},
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    assert r.status_code == 201, r.text


# ----------------------------------------------------------------- 错误分类

def test_error_job_not_found(client):
    r = client.get("/jobs/nope")
    assert r.status_code == 404
    e = r.json()["error"]
    assert e["code"] == "JOB_NOT_FOUND"
    assert e["run_id"]


def test_error_input_invalid_non_frame_aligned(client):
    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "s16",
                  "sample_rate": 1000, "channels": 2}})
    jid = r.json()["job_id"]
    rr = client.post(f"/jobs/{jid}/chunks", content=b"\x00" * 5)
    assert rr.status_code == 422
    e = rr.json()["error"]
    assert e["code"] == "INPUT_INVALID"
    assert e["details"]["bytes_consumed"] == 4
    assert e["details"]["remainder"] == 1


def test_error_invalid_thresholds_at_create(client):
    r = client.post("/jobs", json={
        "config": {"enter_threshold": 0.2, "exit_threshold": 0.1},
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INPUT_INVALID"


def test_error_state_conflict_double_finalize(client):
    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    jid = r.json()["job_id"]
    client.post(f"/jobs/{jid}/finalize")
    rr = client.post(f"/jobs/{jid}/finalize")
    assert rr.status_code == 409
    assert rr.json()["error"]["code"] == "STATE_CONFLICT"


def test_error_resource_exhausted_sample_budget(client):
    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    jid = r.json()["job_id"]
    big = np.zeros(10_001, dtype=np.float32).tobytes()
    rr = client.post(f"/jobs/{jid}/chunks", content=big)
    assert rr.status_code == 413
    assert rr.json()["error"]["code"] == "RESOURCE_EXHAUSTED"


def test_error_resource_exhausted_chunk_bytes(client):
    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    jid = r.json()["job_id"]
    rr = client.post(f"/jobs/{jid}/chunks", content=b"\x00" * 4097)
    assert rr.status_code == 413


def test_bad_wav_is_input_invalid(client):
    r = client.post("/jobs", json={
        "config": CONFIG, "media": {"container": "wav"}})
    jid = r.json()["job_id"]
    rr = client.post(f"/jobs/{jid}/chunks", content=b"definitely not wav")
    assert rr.status_code == 422
    assert rr.json()["error"]["code"] == "INPUT_INVALID"


def test_events_contain_run_id_and_rationale(client):
    x = np.zeros(400, dtype=np.float32)
    x[0:120] = 0.5
    x[200:320] = 0.5
    r = client.post("/jobs", json={
        "config": CONFIG,
        "media": {"container": "pcm", "sample_format": "f32",
                  "sample_rate": 1000, "channels": 1}})
    jid = r.json()["job_id"]
    rid = r.headers["X-Run-ID"]
    client.post(f"/jobs/{jid}/chunks?finalize=true", content=make_pcm(x))
    ev = client.get(f"/jobs/{jid}/events").json()["events"]
    # 至少一条 kernel 事件带 run_closed/判定理由，且带 run_id 可重放
    reasons = [e.get("reason", "") for e in ev]
    assert any("min_silence" in s for s in reasons)
    assert any("min_speech" in s for s in reasons)
    assert all(e["run_id"] for e in ev)
    # 建作业事件带创建时 run_id
    assert any(e["run_id"] == rid for e in ev)
