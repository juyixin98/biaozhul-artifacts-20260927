"""存储层测试：内容寻址幂等、事务原子性、共同祖先与“禁止覆盖历史”。"""
from __future__ import annotations

import json

import pytest

from table_merge.errors import ConflictStateError, NotFoundError
from table_merge.models import TableSchema

from .conftest import EMP_SCHEMA, row

SCHEMA = TableSchema.from_dict(EMP_SCHEMA)
ROWS_A = [row(1, "a", "bj", 10)]
ROWS_B = [row(1, "a", "sh", 11), row(2, "b", "gz", 20)]


def test_snapshot_dedup_returns_same_id_and_no_duplicate_file(store, tmp_path):
    s1 = store.materialize_snapshot(ROWS_A, SCHEMA)
    s2 = store.materialize_snapshot([dict(r) for r in ROWS_A], SCHEMA)
    assert s1.snapshot_id == s2.snapshot_id
    assert s2.reused is True
    files = list((tmp_path / "store" / "snapshots" / "employees").glob("*.parquet"))
    assert len(files) == 1  # 重复导入不产生新文件


def test_commit_and_branch_advance_are_atomic(store):
    s1 = store.materialize_snapshot(ROWS_A, SCHEMA)
    s2 = store.materialize_snapshot(ROWS_B, SCHEMA)
    c1 = store.create_commit(s1.snapshot_id, None, "init", "t")
    store.create_branch("main", c1["commit_id"])

    # 正常线性提交推进分支
    c2 = store.create_commit(s2.snapshot_id, c1["commit_id"], "next", "t",
                             branch_name="main")
    assert store.get_branch("main")["commit_id"] == c2["commit_id"]

    # 父提交不存在 -> 整体失败，分支不动
    with pytest.raises(NotFoundError):
        store.create_commit(s2.snapshot_id, "commit_deadbeef", "x", "t",
                            branch_name="main")
    assert store.get_branch("main")["commit_id"] == c2["commit_id"]

    # 快照不存在 -> 失败，无悬挂提交
    with pytest.raises(NotFoundError):
        store.create_commit("snap_missing", c2["commit_id"], "x", "t")
    assert store.get_commit(c2["commit_id"])["commit_id"]


def test_branch_cannot_be_advanced_with_stale_head(store):
    s1 = store.materialize_snapshot(ROWS_A, SCHEMA)
    s2 = store.materialize_snapshot(ROWS_B, SCHEMA)
    c1 = store.create_commit(s1.snapshot_id, None, "init", "t")
    store.create_branch("main", c1["commit_id"])
    c2 = store.create_commit(s2.snapshot_id, c1["commit_id"], "real next", "t",
                             branch_name="main")
    # 此时 main 已推进到 c2。调用方若仍以旧头 c1 为期望父再推一次（比如重新提交
    # 同一快照），必须被拒绝——不能让分支头回退或覆盖 c2
    with pytest.raises(ConflictStateError) as ei:
        store.create_commit(s2.snapshot_id, c1["commit_id"], "stale", "t",
                            branch_name="main")
    assert ei.value.error_code == "CONFLICT_STATE"
    assert store.get_branch("main")["commit_id"] == c2["commit_id"]  # 未被回退/覆盖


def test_duplicate_branch_rejected(store):
    s1 = store.materialize_snapshot(ROWS_A, SCHEMA)
    c1 = store.create_commit(s1.snapshot_id, None, "init", "t")
    store.create_branch("main", c1["commit_id"])
    with pytest.raises(ConflictStateError):
        store.create_branch("main", c1["commit_id"])


def test_merge_base_along_two_parents_and_merge_commit_two_parents(store):
    s0 = store.materialize_snapshot(ROWS_A, SCHEMA)
    s1 = store.materialize_snapshot(ROWS_B, SCHEMA)
    s2 = store.materialize_snapshot([row(1, "a", "sh", 12)], SCHEMA)
    s3 = store.materialize_snapshot([row(1, "a", "sh", 12), row(3, "c", "hz", 30)],
                                    SCHEMA)

    base_c = store.create_commit(s0.snapshot_id, None, "base", "t")
    store.create_branch("main", base_c["commit_id"])
    store.create_branch("dev", base_c["commit_id"])
    dev_c = store.create_commit(s1.snapshot_id, base_c["commit_id"], "dev", "t",
                                branch_name="dev")
    main_c = store.create_commit(s2.snapshot_id, base_c["commit_id"], "main", "t",
                                 branch_name="main")

    mb = store.find_merge_base(dev_c["commit_id"], main_c["commit_id"])
    assert mb["base_commit_id"] == base_c["commit_id"]
    assert mb["base_snapshot_id"] == s0.snapshot_id

    merged_c = store.create_merge_commit(
        snapshot_id=s3.snapshot_id,
        base_commit_id=base_c["commit_id"],
        parent1_dev_commit_id=dev_c["commit_id"],
        parent2_main_commit_id=main_c["commit_id"],
        base_snapshot_id=s0.snapshot_id,
        parent1_snapshot_id=s1.snapshot_id,
        parent2_snapshot_id=s2.snapshot_id,
        resolution_summary={"resolved": 0},
        target_branch="main",
        expected_head_commit_id=main_c["commit_id"],
        message="merge", author="t",
    )
    assert merged_c["parent_commit_ids"] == [dev_c["commit_id"], main_c["commit_id"]]
    assert merged_c["merge"]["base_commit_id"] == base_c["commit_id"]
    assert store.get_branch("main")["commit_id"] == merged_c["commit_id"]

    # 合并提交必须同时是两条线的祖先可达点
    ancestors = store.ancestor_snapshots(merged_c["commit_id"])
    assert dev_c["commit_id"] in ancestors and main_c["commit_id"] in ancestors
    assert base_c["commit_id"] in ancestors


def test_reimporting_main_snapshot_cannot_overwrite_history(store):
    """题目硬性要求：不允许用重新导入主分支快照覆盖分支历史。

    重新导入相同内容只命中去重；导入不同内容只能产生新提交追加到分支，
    无法修改既有提交或其父边。
    """
    s1 = store.materialize_snapshot(ROWS_A, SCHEMA)
    c1 = store.create_commit(s1.snapshot_id, None, "init", "t")
    store.create_branch("main", c1["commit_id"])

    # 相同内容再导一次：同一快照 ID
    assert store.materialize_snapshot(ROWS_A, SCHEMA).snapshot_id == s1.snapshot_id

    # 试图把 c1 的父边改成别的 -> 存储层不提供改写 API；直接 SQL 篡改应被外键/
    # 不可变约定挡住。这里验证服务历史记录仍只有原始一条边
    assert store.get_commit(c1["commit_id"])["parent_commit_ids"] == []

    s2 = store.materialize_snapshot(ROWS_B, SCHEMA)
    c2 = store.create_commit(s2.snapshot_id, c1["commit_id"], "append", "t",
                             branch_name="main")
    assert c2["parent_commit_id"] == c1["commit_id"]
    assert store.get_commit(c1["commit_id"])["snapshot_id"] == s1.snapshot_id  # 未被改写


def test_resolutions_are_persisted_bound_to_three_snapshots(store):
    s1 = store.materialize_snapshot(ROWS_A, SCHEMA)
    c1 = store.create_commit(s1.snapshot_id, None, "init", "t")
    store.create_branch("main", c1["commit_id"])

    items = [{
        "row_key": "[4]", "decision": "DELETE_MODIFY_CONFLICT", "action": "USE_MAIN",
        "field_picks": None,
        "base_snapshot_id": "snap_b", "dev_snapshot_id": "snap_d",
        "main_snapshot_id": "snap_m",
    }]
    assert store.save_resolutions("plan_x", items) == 1
    # 幂等：同一 plan+row_key 再存为更新
    items[0]["action"] = "KEEP_DELETED"
    store.save_resolutions("plan_x", items)
    loaded = store.load_resolutions("plan_x")
    assert loaded["[4]"]["action"] == "KEEP_DELETED"
    assert loaded["[4]"]["base_snapshot_id"] == "snap_b"
    assert json.dumps(loaded["[4]"])  # 可序列化
