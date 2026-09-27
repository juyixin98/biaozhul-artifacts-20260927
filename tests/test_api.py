"""API 与作业状态机测试（FastAPI TestClient + SQLite）。"""

from fractions import Fraction

import pytest


def test_health_reports_versions(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service_version"]
    # 依赖版本字段必须存在（可追溯环境）
    assert set(body["dependencies"]) == {"fastapi", "numpy", "pydantic"}
    assert body["python"].count(".") == 2


def test_job_success_lifecycle(client, generated_dir, testlog):
    path = generated_dir / "bframes.mp4"
    resp = client.post("/jobs", json={"path": str(path)})
    assert resp.status_code == 201
    job = resp.json()
    job_id = job["job_id"]
    assert job["status"] == "done"
    assert job["input_sha256"]
    log_text = " ".join(line["message"] for line in job["logs"])
    assert "status -> running" in log_text and "status -> done" in log_text
    testlog.write(
        {
            "event": "assertion",
            "test": "job_success_lifecycle",
            "basis": "作业必须经 queued->running->done，日志含 sha256 与进度",
            "actual": {"job_id": job_id, "status": job["status"], "log_count": len(job["logs"])},
        }
    )

    result = client.get(f"/jobs/{job_id}/result").json()
    pres = result["tracks"][0]["presentations"]
    assert [p["sample_index"] for p in pres] == [0, 2, 3, 1, 5, 4]
    assert pres[1]["movie_time"] == "100"
    assert pres[1]["playback_interval_seconds"] == ["1/10", "1/5"]
    # 字节范围回传
    assert pres[0]["byte_range"][1] == 500


def test_job_failure_classified_not_faked_success(client, generated_dir, testlog):
    """坏文件的作业必须 failed 并带错误类别；结果接口返回 422 而非成功载荷。"""

    path = generated_dir / "bad_length.mp4"
    job = client.post("/jobs", json={"path": str(path)}).json()
    assert job["status"] == "failed"
    assert job["error_class"] == "BoxLengthError"
    testlog.write(
        {
            "event": "assertion",
            "test": "job_failure_classified",
            "basis": "越界盒必须记 failed + BoxLengthError，不允许伪装成功",
            "actual": {"status": job["status"], "error_class": job["error_class"]},
        }
    )
    resp = client.get(f"/jobs/{job['job_id']}/result")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_class"] == "BoxLengthError"

    # fragmented 按 UnsupportedLayoutError 分类
    job2 = client.post("/jobs", json={"path": str(generated_dir / "fragmented.mp4")}).json()
    assert job2["status"] == "failed"
    assert job2["error_class"] == "UnsupportedLayoutError"


def test_job_not_found_and_path_guard(client, app_settings):
    assert client.get("/jobs/nonexistent").status_code == 404
    # 允许目录之外的路径必须拒绝
    resp = client.post("/jobs", json={"path": "/etc/hostname"})
    assert resp.status_code == 403
    resp = client.post("/jobs", json={"path": str(app_settings.fixtures_dir / "missing.mp4")})
    assert resp.status_code == 404


def test_validate_endpoint_all_pass(client, testlog):
    resp = client.get("/validate")
    assert resp.status_code == 200
    report = resp.json()
    statuses = {f["fixture"]: f["status"] for f in report["fixtures"]}
    assert statuses == {
        "bframes.mp4": "pass",
        "empty_edit.mp4": "pass",
        "trim_edit.mp4": "pass",
        "multi_edit.mp4": "pass",
        "multitrack.mp4": "pass",
    }
    testlog.write(
        {
            "event": "assertion",
            "test": "validate_endpoint",
            "basis": "5 个夹具全部对照手写参考与载荷 sha1 通过",
            "actual": statuses,
        }
    )
    assert report["overall"] == "pass"
