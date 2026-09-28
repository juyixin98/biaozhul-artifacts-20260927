"""原子提交：全部动作先验证再提交；注入提交失败后无部分更新。

覆盖三个注入点：
  after_actions  行改动已执行但未 COMMIT -> ROLLBACK（最强的“无部分更新”）
  before_commit  COMMIT 前抛 ENOSPC      -> RESOURCE_EXHAUSTED/DISK_FULL + 回滚
  commit_raises  commit() 自身抛磁盘错误 -> COMPUTATION_FAILURE/COMMIT_FAILED + 回滚
"""
from __future__ import annotations

import pytest

from merge_engine import MergeRequest

from conftest import cfg, seed

pytestmark = pytest.mark.capture


def _mixed_batch_request():
    """一次运行同时含 UPDATE + INSERT + DELETE，最大化暴露部分更新。"""
    return MergeRequest(
        source={"format": "records", "records": [
            {"k1": "u", "k2": 1, "v": "updated"},
            {"k1": "i", "k2": 2, "v": "inserted"},
        ]},
        config=cfg(
            "atom", ("k1", "k2"),
            delete_unmatched=True,
            delete={"op": "eq", "left": {"side": "target", "column": "v"},
                    "right": {"literal": "doomed"}},
        ),
    )


def _preimage(eng):
    return eng.get_target_rows("atom")


@pytest.mark.parametrize(
    "fault_point, expected_code, expected_category",
    [
        ("after_actions", "COMMIT_FAILED", "COMPUTATION_FAILURE"),
        ("before_commit", "DISK_FULL", "RESOURCE_EXHAUSTED"),
        ("commit_raises", "COMMIT_FAILED", "COMPUTATION_FAILURE"),
    ],
)
def test_commit_failure_leaves_no_partial_update(
    tmp_path, fault_point, expected_code, expected_category
):
    eng = seed(tmp_path, "atom", ["k1", "k2", "v"], [
        {"k1": "u", "k2": 1, "v": "original"},   # 会被 UPDATE
        {"k1": "g", "k2": 3, "v": "survivor"},   # 保留
        {"k1": "d", "k2": 4, "v": "doomed"},     # 会被 DELETE
    ])
    before = _preimage(eng)

    req = _mixed_batch_request()
    req = MergeRequest(source=req.source, config=req.config, fault_point=fault_point)
    result = eng.run(req)

    # 1) 结果分类明确
    assert result.status == "FAILED"
    assert result.error["code"] == expected_code
    assert result.error["category"] == expected_category

    # 2) 目标表逐字节回到操作前
    after = _preimage(eng)
    assert after == before, f"partial update detected for {fault_point}: {after}"

    # 3) 审计动作没有任何一行落盘（actions 表随事务回滚）
    assert eng.get_actions(result.run_id) == []

    # 4) 运行被登记为 FAILED，保留指纹与计划计数用于重放
    meta = eng.get_run(result.run_id)
    assert meta["status"] == "FAILED"
    assert meta["snapshot_fingerprint"] == result.plan.snapshot_fingerprint
    assert meta["error"]["code"] == expected_code


def test_failed_run_can_be_replayed_after_clearing_fault(tmp_path):
    """失败 -> 无部分更新 -> 去掉注入后用相同输入重放必须成功且动作集合一致。"""
    eng = seed(tmp_path, "atom", ["k1", "k2", "v"], [
        {"k1": "u", "k2": 1, "v": "original"},
        {"k1": "g", "k2": 3, "v": "survivor"},
        {"k1": "d", "k2": 4, "v": "doomed"},
    ])
    bad = MergeRequest(source={"format": "records", "records": [
        {"k1": "u", "k2": 1, "v": "updated"},
        {"k1": "i", "k2": 2, "v": "inserted"},
    ]}, config=cfg(
        "atom", ("k1", "k2"), delete_unmatched=True,
        delete={"op": "eq", "left": {"side": "target", "column": "v"},
                "right": {"literal": "doomed"}},
    ), fault_point="after_actions")
    failed = eng.run(bad)
    assert failed.status == "FAILED"

    good = MergeRequest(source=bad.source, config=bad.config)
    ok = eng.run(good)
    assert ok.status == "COMMITTED"
    assert ok.plan.write_counts == {
        "UPDATE_MATCHED": 1, "INSERT_UNMATCHED": 1, "DELETE_UNMATCHED": 1,
    }
    # 两次决策基于同一操作前快照（失败未改数据），指纹一致
    assert ok.plan.snapshot_fingerprint == failed.plan.snapshot_fingerprint


def test_dry_run_never_writes_target(tmp_path):
    eng = seed(tmp_path, "atom", ["k1", "k2", "v"], [
        {"k1": "u", "k2": 1, "v": "original"},
    ])
    before = eng.get_target_rows("atom")
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "u", "k2": 1, "v": "x"},
                                                 {"k1": "n", "k2": 9, "v": "y"}]},
        config=cfg("atom", ("k1", "k2")), dry_run=True,
    )
    result = eng.run(req)
    assert result.status == "PLANNED"
    assert result.dry_run is True
    assert eng.get_target_rows("atom") == before
    # dry-run 不产生动作审计落盘，但运行登记为 PLANNED
    assert eng.get_run(result.run_id)["status"] == "PLANNED"
    assert eng.get_actions(result.run_id) == []


def test_dry_run_references_existing_target_columns(tmp_path):
    """dry-run 不建表，但已存在目标表的列必须可用于 delete/update 条件校验。"""
    eng = seed(tmp_path, "atomdr", ["k1", "k2", "v", "status"], [
        {"k1": "u", "k2": 1, "v": 1, "status": "STALE"},
        {"k1": "g", "k2": 2, "v": 2, "status": "KEEP"},
    ])
    before = eng.get_target_rows("atomdr")
    req = MergeRequest(
        source={"format": "records", "records": []},
        config=cfg("atomdr", ("k1", "k2"),
                   delete_unmatched=True,
                   delete={"op": "eq", "left": {"side": "target", "column": "status"},
                           "right": {"literal": "STALE"}}),
        dry_run=True,
    )
    result = eng.run(req)
    assert result.status == "PLANNED"
    # 空源批：两个目标行都未匹配，STALE 行应计划删除（dry-run 只决策）
    deletes = [a for a in result.plan.actions if a.type.value == "DELETE_UNMATCHED"]
    assert len(deletes) == 1 and deletes[0].target_rowid == 1
    assert eng.get_target_rows("atomdr") == before


def test_actions_table_only_commits_with_target_changes(tmp_path):
    eng = seed(tmp_path, "atom", ["k1", "k2", "v"], [
        {"k1": "u", "k2": 1, "v": "original"},
    ])
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "u", "k2": 1, "v": "updated"}]},
        config=cfg("atom", ("k1", "k2")),
    )
    result = eng.run(req)
    actions = eng.get_actions(result.run_id)
    types = [a["type"] for a in actions]
    assert types == ["UPDATE_MATCHED"]
    assert actions[0]["before"] == {"k1": "u", "k2": 1, "v": "original"}
    assert actions[0]["after"] == {"k1": "u", "k2": 1, "v": "updated"}
    assert actions[0]["key"] == ["u", 1]
    assert actions[0]["reason"] == "MATCHED_UPDATE_COND_TRUE"
    assert actions[0]["target_rowid"] == 1
