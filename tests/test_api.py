"""端到端 HTTP/服务测试：作业状态、失败类别、幂等冲突、资源耗尽、复现日志。"""

from __future__ import annotations

import io
import json
import wave

import numpy as np
import pytest

from app.errors import SegmentError
from app.media import parse_audio


def _wav(samples_int16: np.ndarray, rate: int = 1000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples_int16.tobytes())
    return buf.getvalue()


def _signal_file() -> bytes:
    # 与 test_segmentation 的跨块长静音同构：100 LOUD + 600 LOW + 100 LOUD
    sig = np.array([0.5] * 100 + [0.0] * 600 + [0.5] * 100)
    return _wav((sig * 32767).astype("<i2"))


CONFIG = {
    "min_silence_ms": 300,
    "min_activity_ms": 100,
    "pad_ms": 50,
    "merge_gap_ms": 120,
    "enter_threshold": 0.02,
    "exit_threshold": 0.05,
}


class TestJobHappyPath:
    def test_job_returns_verified_intervals(self, client):
        resp = client.post(
            "/jobs",
            files={"file": ("a.wav", _signal_file(), "audio/wav")},
            data={"config": json.dumps(CONFIG)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "SUCCEEDED"
        assert body["sample_rate"] == 1000
        assert body["total_samples"] == 800
        assert body["intervals"] == [[0, 150], [650, 800]]
        assert body["raw_ranges"] == [[0, 100], [700, 800]]
        assert sum(e - s for s, e in body["intervals"]) == 300

    def test_service_chunk_size_does_not_change_result(self, service):
        # 直接用 service，分别用极小与默认内部块大小，结果必须一致。
        data = _signal_file()
        cfg = {**CONFIG, "fmt": "auto", "sample_rate": None}
        service.stream_chunk_samples = 1
        r1 = service.submit(data, cfg)
        service.stream_chunk_samples = 4000
        r2 = service.submit(data, cfg)
        assert r1.intervals == r2.intervals == [[0, 150], [650, 800]]

    def test_get_and_list(self, client):
        create = client.post(
            "/jobs",
            files={"file": ("a.wav", _signal_file(), "audio/wav")},
            data={"config": json.dumps(CONFIG)},
        )
        job_id = create.json()["job_id"]
        got = client.get(f"/jobs/{job_id}")
        assert got.status_code == 200 and got.json()["intervals"] == [[0, 150], [650, 800]]
        listed = client.get("/jobs").json()["jobs"]
        assert any(j["job_id"] == job_id for j in listed)


class TestInputFailures:
    def test_bad_media_is_parse_error(self, client):
        resp = client.post(
            "/jobs",
            files={"file": ("x.wav", b"RIFFnotreallywave", "audio/wav")},
            data={"config": json.dumps({**CONFIG, "fmt": "wav"})},
        )
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert err["code"] == "MEDIA_PARSE_ERROR"
        assert err["category"] == "input"

    def test_empty_upload(self, client):
        resp = client.post(
            "/jobs",
            files={"file": ("x.wav", b"", "audio/wav")},
            data={"config": json.dumps(CONFIG)},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "EMPTY_INPUT"

    def test_bad_config_json(self, client):
        resp = client.post(
            "/jobs",
            files={"file": ("a.wav", _signal_file(), "audio/wav")},
            data={"config": "{not json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_threshold_order_validation(self, client):
        cfg = {**CONFIG, "enter_threshold": 0.9, "exit_threshold": 0.1}
        resp = client.post(
            "/jobs",
            files={"file": ("a.wav", _signal_file(), "audio/wav")},
            data={"config": json.dumps(cfg)},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_pydantic_shape_error(self, client):
        cfg = {**CONFIG, "min_silence_ms": -1}
        resp = client.post(
            "/jobs",
            files={"file": ("a.wav", _signal_file(), "audio/wav")},
            data={"config": json.dumps(cfg)},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_ARGUMENT"

    def test_failed_job_status_is_queryable(self, client, service):
        resp = client.post(
            "/jobs",
            files={"file": ("x.wav", b"RIFFnotreallywave", "audio/wav")},
            data={"config": json.dumps({**CONFIG, "fmt": "wav"})},
        )
        assert resp.status_code == 400
        # 解析失败发生在执行期：作业落为 FAILED 且带失败类别。
        failed = service.store.list_jobs()
        assert len(failed) == 1 and failed[0].status == "FAILED"
        got = client.get(f"/jobs/{failed[0].id}")
        assert got.status_code == 200
        assert got.json()["error"]["category"] == "input"
        assert got.json()["error"]["code"] == "MEDIA_PARSE_ERROR"

    def test_computation_failure_persists_failed_job(self, service):
        raw = np.array([0.0, np.nan, 0.5], dtype="<f4").tobytes()
        cfg = {**CONFIG, "fmt": "raw:f32le", "sample_rate": 1000}
        with pytest.raises(SegmentError) as ei:
            service.submit(raw, cfg)
        assert ei.value.code == "COMPUTATION_FAILED"
        jobs = service.store.list_jobs()
        failed = [j for j in jobs if j.status == "FAILED"]
        assert len(failed) == 1
        assert failed[0].error_category == "computation"
        assert failed[0].error_code == "COMPUTATION_FAILED"


class TestResourceExhaustion:
    def test_sample_budget(self, service):
        service.max_samples_per_job = 50
        with pytest.raises(SegmentError) as ei:
            service.submit(_signal_file(), {**CONFIG, "fmt": "auto", "sample_rate": None})
        assert ei.value.code == "RESOURCE_EXHAUSTED"
        assert ei.value.category == "resource"
        assert ei.value.details["limit"] == 50

    def test_byte_budget_http(self, service, client):
        service.max_upload_bytes = 10
        resp = client.post(
            "/jobs",
            files={"file": ("a.wav", b"\x00" * 11, "audio/wav")},
            data={"config": json.dumps(CONFIG)},
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "RESOURCE_EXHAUSTED"


class TestStateConflicts:
    def test_job_not_found(self, client):
        resp = client.get("/jobs/job-doesnotexist")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"

    def test_run_trace_not_found(self, client):
        resp = client.get("/runs/run-missing")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"

    def test_same_idempotency_key_replays_job(self, client):
        files = {"file": ("a.wav", _signal_file(), "audio/wav")}
        data = {"config": json.dumps(CONFIG)}
        h1 = {"Idempotency-Key": "key-1"}
        r1 = client.post("/jobs", files=files, data=data, headers=h1)
        r2 = client.post("/jobs", files=files, data=data, headers=h1)
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["job_id"] == r2.json()["job_id"]

    def test_idempotency_key_with_different_payload_conflicts(self, client):
        files = {"file": ("a.wav", _signal_file(), "audio/wav")}
        r1 = client.post(
            "/jobs", files=files, data={"config": json.dumps(CONFIG)},
            headers={"Idempotency-Key": "key-2"},
        )
        assert r1.status_code == 200
        cfg2 = {**CONFIG, "min_silence_ms": 500}
        r2 = client.post(
            "/jobs", files=files, data={"config": json.dumps(cfg2)},
            headers={"Idempotency-Key": "key-2"},
        )
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "STATE_CONFLICT"
        assert r2.json()["error"]["category"] == "state"


class TestReplayLog:
    def test_run_log_carries_midstate_and_reason(self, service):
        rec = service.submit(
            _signal_file(), {**CONFIG, "fmt": "auto", "sample_rate": None}
        )
        entry = service.trace(rec.run_id)
        assert entry["run_id"] == rec.run_id
        assert entry["status"] == "SUCCEEDED"
        assert entry["failure_category"] is None
        assert entry["result"]["intervals"] == [[0, 150], [650, 800]]
        # 关键中间状态与判断理由在场（足以配合保存的字节重放）
        assert entry["final_state"]["state"] == "active"  # 末尾未完成段
        kinds = {e.get("event") for e in entry["events_tail"]}
        assert "raw_range" in kinds
        reasons = {
            e.get("reason")
            for e in entry["events_tail"]
            if e.get("event") == "raw_range"
        }
        assert "trailing_open" in reasons

    def test_failure_log_has_category(self, service):
        with pytest.raises(SegmentError):
            service.submit(b"", {**CONFIG, "fmt": "auto", "sample_rate": None})
        # 空字节在落盘前拒绝（无作业）；改用 NaN 产生一条 FAILED 运行日志
        raw = np.array([np.nan], dtype="<f4").tobytes()
        with pytest.raises(SegmentError):
            service.submit(raw, {**CONFIG, "fmt": "raw:f32le", "sample_rate": 1000})
        failed_entries = [
            e for e in service.runs.read_all() if e["status"] == "FAILED"
        ]
        assert failed_entries and failed_entries[-1]["failure_category"] == "computation"
        assert failed_entries[-1]["error"]["code"] == "COMPUTATION_FAILED"
