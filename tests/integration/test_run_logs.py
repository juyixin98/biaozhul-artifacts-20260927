"""运行日志测试：日志必须能关联输入/运行身份，显示版本、步骤、判定与失败状态。"""
from __future__ import annotations

import json

import pytest

from merge3.errors import UnresolvedConflictError


def _setup(svc):
    fields = [
        {"name": "id", "type": "int64", "nullable": False},
        {"name": "status", "type": "string"},
    ]
    svc.register_table("t", ["id"], fields)
    base = svc.write_snapshot("t", [{"id": 1, "status": "new"}])
    svc.create_branch("t", "main", base.snapshot_id)
    svc.create_branch("t", "develop", base.snapshot_id)
    svc.commit_rows("t", "develop", [{"id": 1, "status": "paid"}])
    svc.commit_rows("t", "main", [{"id": 1, "status": "void"}])


def _read_log(svc, run_id):
    path = svc.cfg.log_dir / f"run-{run_id}.jsonl"
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    return lines


def test_log_links_run_id_inputs_version_and_decisions(svc):
    _setup(svc)
    run = svc.start_merge("t")
    lines = _read_log(svc, run.run_id)
    assert all(l["run_id"] == run.run_id for l in lines)          # 可关联运行身份
    start = next(l for l in lines if l["stage"] == "merge_start")
    assert start["service_version"]                                # 版本可见
    assert start["input_snapshots"]["base_ancestor"] == run.base_snapshot_id
    assert start["input_snapshots"]["ours_develop"] == run.ours_snapshot_id
    assert start["input_snapshots"]["theirs_main"] == run.theirs_snapshot_id

    done = next(l for l in lines if l["stage"] == "classify_done")
    assert done["totals"] == {"auto": 0, "conflict": 1, "total": 1}
    # 判定依据逐条可见
    key_detail = done["per_key"]["[1]"]
    assert key_detail["classification"] == "field_value_conflict"
    assert key_detail["conflict"] is True and key_detail["reason"]

    svc.resolve_conflict(run.run_id, [1], "ours", binding=run.binding())
    lines = _read_log(svc, run.run_id)
    res = next(l for l in lines if l["stage"] == "conflict_resolved")
    assert res["resolution"] == "ours"
    assert res["bound_to_snapshots"]["base"]  # 解决记录回链三方快照（短ID）

    snap = svc.commit_merge(run.run_id, message="ok")
    lines = _read_log(svc, run.run_id)
    committed = next(l for l in lines if l["stage"] == "merge_committed")
    assert committed["status"] == "ok"
    assert committed["parent_refs"] == [run.ours_snapshot_id, run.theirs_snapshot_id]
    assert committed["row_count"] == 1
    assert committed["branch_advanced"]["to"] == snap.snapshot_id


def test_log_records_failure_not_success(svc):
    _setup(svc)
    run = svc.start_merge("t")
    with pytest.raises(UnresolvedConflictError):
        svc.commit_merge(run.run_id)
    lines = _read_log(svc, run.run_id)
    failed = [l for l in lines if l.get("status") == "failed"]
    assert failed and failed[-1]["error"]["code"] == "unresolved_conflicts"
    # 没有任何一条把该次提交记成 ok
    assert not any(l["stage"] == "merge_committed" for l in lines)


def test_log_records_binding_mismatch_and_rejection(svc):
    _setup(svc)
    run = svc.start_merge("t")
    from merge3.errors import BindingMismatchError, ResolutionRejectedError
    with pytest.raises(BindingMismatchError):
        svc.resolve_conflict(run.run_id, [1], "ours",
                             binding=(run.base_snapshot_id, run.ours_snapshot_id, "nope"))
    with pytest.raises(ResolutionRejectedError):
        svc.resolve_conflict(run.run_id, [99], "ours", binding=run.binding())
    lines = _read_log(svc, run.run_id)
    codes = {l["error"]["code"] for l in lines if "error" in l}
    assert "resolution_binding_mismatch" in codes
    assert "resolution_rejected" in codes
