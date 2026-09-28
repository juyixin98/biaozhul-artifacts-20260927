"""FastAPI 端到端测试：正常计划、失败类别、异常不伪装成功、校验视图可逐样本复核。"""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "api_jobs.db"
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("MEDIACONCAT_DB", str(db))
    monkeypatch.setenv("MEDIACONCAT_LOG_DIR", str(log_dir))
    monkeypatch.setenv("MEDIACONCAT_FIXTURES", "fixtures")
    import mediaconcat.api as api_mod
    importlib.reload(api_mod)  # 确保 lifespan 使用新环境
    with TestClient(api_mod.app) as c:
        yield c


def _post(client, sources, container="mp4", cuts=None, job_id=None):
    body = {"sources": sources, "output_container": container}
    if cuts is not None:
        body["cuts"] = cuts
    if job_id is not None:
        body["job_id"] = job_id
    return client.post("/jobs", json=body)


def test_health_reports_version_and_run(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["version"]


def test_happy_path_concat_plan_is_inspectable(client):
    r = _post(client, ["tb_mixed_a", "tb_mixed_b"], "mp4", job_id="api-1")
    assert r.status_code == 201
    detail = client.get(r.json()["detail"]).json()
    assert detail["status"] == "succeeded"
    plan = detail["plan"]
    assert plan["feasible"] is True and plan["mode"] == "concat_copy"
    assert plan["output_time_base"] == [1, 150]
    # 逐样本可检验：14 个视频样本
    vids = [s for seg in plan["segments"] for s in seg["samples"]]
    assert len(vids) == 14
    assert all(s["out_dts"] >= 0 for s in vids)
    # 拼接缝：第二段起点 42，第一段最后 36+6=42
    assert vids[6]["out_dts"] + vids[6]["duration"] == vids[7]["out_dts"] == 42


def test_verify_endpoint_summarizes_checks(client):
    _post(client, ["open_gop"], "mp4", job_id="api-og")
    r = client.get("/jobs/api-og/verify").json()
    assert r["mode"] == "concat_copy"
    check = next(c for c in r["checks"] if c["stream"] == "video")
    assert check["nonnegative"] is True and check["strict_increasing"] is True
    assert check["roles"]["preroll_reference"] == 6
    assert r["warnings"]  # 非关键帧起切有明确 warning


def test_transcode_required_is_still_succeeded_job_with_false_feasible(client):
    # 作业本身执行成功（规划完成），但结论是“必须转码”，不得伪装成可直拼
    _post(client, ["nonkey_a", "nonkey_b"], "mp4", job_id="api-2")
    detail = client.get("/jobs/api-2").json()
    assert detail["status"] == "succeeded"
    plan = detail["plan"]
    assert plan["feasible"] is False
    assert plan["mode"] == "transcode_required"
    codes = [e["code"] for e in client.get("/jobs/api-2/verify").json()["errors"]]
    assert codes == ["non_keyframe_cut"]


def test_missing_input_is_failed_job_not_success(client):
    # 未知输入：服务捕获 ProbeError → status=failed（不是 201+succeeded）
    r = _post(client, ["no_such_clip_xyz"], "mp4", job_id="api-3")
    assert r.status_code == 201
    detail = client.get("/jobs/api-3").json()
    assert detail["status"] == "failed"
    assert detail["plan"] is None
    assert detail["error_code"] == "input_not_found"


def test_unexpected_exception_is_internal_error_not_success(client, monkeypatch):
    # 未知异常必须落 internal_error，而不是被吞掉后返回成功
    from mediaconcat import service as svc

    def boom(*a, **k):
        raise RuntimeError("unexpected kernel boom")

    monkeypatch.setattr(svc, "plan_concat", boom)
    r = _post(client, ["av_tailpad"], "mp4", job_id="api-4")
    assert r.status_code == 201
    detail = client.get("/jobs/api-4").json()
    assert detail["status"] == "failed"
    assert detail["error_code"] == "internal_error"
    assert "unexpected kernel boom" in detail["error"]


def test_unknown_job_returns_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/verify").status_code == 404


def test_rejected_container_pattern_422(client):
    r = client.post("/jobs", json={"sources": ["a"], "output_container": "avi"})
    assert r.status_code == 422


def test_tail_padding_warning_visible_in_verify(client):
    _post(client, ["av_tailpad"], "mp4", job_id="api-pad")
    v = client.get("/jobs/api-pad/verify").json()
    assert v["feasible"] is True
    audio_check = next(c for c in v["checks"] if c["stream"] == "audio")
    assert audio_check["roles"]["silence_pad"] == 3
    assert any(w["code"] == "av_duration_mismatch" for w in v["warnings"])


def test_log_file_correlates_run_and_job(tmp_path, client):
    _post(client, ["av_tailpad"], "mp4", job_id="api-log")
    logs = list((tmp_path / "logs").glob("*.jsonl"))
    assert logs, "必须写出 JSONL 运行日志"
    lines = [json.loads(x) for x in logs[0].read_text(encoding="utf-8").splitlines()]
    job_events = [x for x in lines if x.get("job_id") == "api-log"]
    assert job_events, "日志必须能按 job_id 关联"
    assert all(x["run_id"] for x in lines) and all(x["version"] for x in lines)
    # 至少包含进度与最终判定
    assert any("progress" in x for x in job_events)
    finals = [x for x in job_events if x.get("decision")]
    assert finals and finals[-1]["decision"] == "concat_copy"
