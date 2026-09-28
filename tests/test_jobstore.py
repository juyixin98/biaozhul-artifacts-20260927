"""作业状态机（SQLite）测试：成功与失败路径都持久化、可关联 run_id。"""
from __future__ import annotations

import json

from mediaconcat.jobstore import JobStore


def test_job_lifecycle_success(tmp_path):
    store = JobStore(str(tmp_path / "j.db"), run_id="run-xyz")
    store.create("j1", ["a", "b"], "mp4")
    assert store.get("j1")["status"] == "queued"
    store.update_status("j1", "planning")
    plan = {"job_id": "j1", "feasible": True}
    store.save_plan("j1", json.dumps(plan))
    row = store.get("j1")
    assert row["status"] == "succeeded"
    assert json.loads(row["plan_json"])["feasible"] is True
    assert row["run_id"] == "run-xyz"
    assert any(j["job_id"] == "j1" for j in store.list_jobs())
    store.close()


def test_job_lifecycle_failure_is_persisted(tmp_path):
    store = JobStore(str(tmp_path / "j.db"), run_id="run-err")
    store.create("j2", ["x"], "mp4")
    store.fail("j2", "internal_error", "boom")
    row = store.get("j2")
    # 失败显式落盘，而不是被标记为 succeeded
    assert row["status"] == "failed"
    assert row["error_code"] == "internal_error"
    assert "boom" in row["error"]
    assert row["plan_json"] is None
    store.close()


def test_invalid_status_rejected(tmp_path):
    store = JobStore(str(tmp_path / "j.db"), run_id="run-s")
    store.create("j3", ["x"], "mp4")
    try:
        store.update_status("j3", "totally_fine_honest")  # 不允许虚构状态
        raise AssertionError("应拒绝非法状态")
    except ValueError:
        pass
    store.close()
