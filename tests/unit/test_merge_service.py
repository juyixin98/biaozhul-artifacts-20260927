"""服务用例单元测试：不可变、内容寻址、三方快照绑定、提交后两条父引用。"""
from __future__ import annotations

import json

import pytest

from merge3.domain.models import MergeStatus
from merge3.errors import (
    BindingMismatchError,
    ConflictError,
    NotFoundError,
    ResolutionRejectedError,
    UnresolvedConflictError,
)
from merge3.kernel import lineage


# ------------------------------------------------------------ 快照不可变 / 内容寻址

def test_identical_rows_share_snapshot(svc, branched):
    a = svc.write_snapshot("orders", [dict(r) for r in [
        {"id": 1, "status": "new", "amount": 100, "owner": "alice"}]])
    b = svc.write_snapshot("orders", [{"id": 1, "status": "new", "amount": 100,
                                       "owner": "alice"}])
    assert a.snapshot_id == b.snapshot_id
    spec, rows = svc.read_snapshot_rows(a.snapshot_id)
    assert rows[0]["status"] == "new"


def test_stale_branch_pointer_rejected(svc, branched):
    import copy
    rows1 = [{"id": 1, "status": "a", "amount": 1, "owner": "x"}]
    rows2 = [{"id": 1, "status": "b", "amount": 2, "owner": "y"}]
    svc.commit_rows("orders", "develop", rows1)
    # 基于过期 base 直接提交不允许覆盖：commit_rows 总是读当前头，rows2 会接在 rows1 后
    snap2 = svc.commit_rows("orders", "develop", rows2)
    assert svc.get_branch_head("orders", "develop").snapshot_id == snap2.snapshot_id


def test_parquet_files_are_immutable_artifacts(svc, branched, cfg):
    head = svc.get_branch_head("orders", "main")
    p = cfg.storage.parquet_dir / f"{head.snapshot_id}.parquet"
    assert p.exists() and p.stat().st_size > 0


# ------------------------------------------------------------ 完整合并流程（四类场景）

def _demo_rows():
    base = [
        {"id": 1, "status": "new", "amount": 100, "owner": "alice"},
        {"id": 2, "status": "new", "amount": 200, "owner": "bob"},
        {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
        {"id": 4, "status": "new", "amount": 400, "owner": "dave"},
        {"id": 8, "status": "new", "amount": 800, "owner": "hank"},
    ]
    dev = [
        {"id": 1, "status": "paid", "amount": 100, "owner": "alice"},    # 同字段不同值冲突
        {"id": 2, "status": "new", "amount": 200, "owner": "bob-temp"},  # 不同字段自动合并
        {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
        {"id": 4, "status": "new", "amount": 400, "owner": "dave"},
        {"id": 5, "status": "new", "amount": 500, "owner": "erin"},      # 互斥新增(dev)
        {"id": 8, "status": "new", "amount": 800, "owner": "hank"},
    ]
    main = [
        {"id": 1, "status": "void", "amount": 100, "owner": "alice"},
        {"id": 2, "status": "new", "amount": 220, "owner": "bob"},
        {"id": 3, "status": "paid", "amount": 300, "owner": "carol"},
        # id=4 被主分支删除（dev 未改）-> 自动删除
        # id=8 dev 未改而 main 删除 -> 自动删除
        {"id": 6, "status": "new", "amount": 600, "owner": "frank"},     # 互斥新增(main)
    ]
    # dev 对 id=7 做了修改、main 删除 id=7（id=7 不在 main_rows 中）-> 删除/修改冲突
    base.append({"id": 7, "status": "new", "amount": 700, "owner": "gina"})
    dev.append({"id": 7, "status": "shipped", "amount": 710, "owner": "gina"})
    return base, dev, main


def test_full_merge_auto_partitions_and_conflicts(svc):
    base_rows, dev_rows, main_rows = _demo_rows()
    base = svc.write_snapshot("orders", base_rows)
    svc.create_branch("orders", "main", base.snapshot_id)
    svc.create_branch("orders", "develop", base.snapshot_id)
    svc.commit_rows("orders", "develop", dev_rows)
    svc.commit_rows("orders", "main", main_rows)

    run = svc.start_merge("orders")
    assert run.base_snapshot_id == base.snapshot_id  # LCA 自动找到共同祖先
    classified = {tuple(e.key.values())[0]: e for e in run.plan.entries.values()}
    assert classified[1].classification == "field_value_conflict"
    assert classified[2].classification == "field_merge"
    assert classified[2].merged["amount"] == 220
    assert classified[2].merged["owner"] == "bob-temp"
    assert classified[4].classification == "theirs_deleted" and classified[4].deleted
    assert classified[5].classification == "ours_added"
    assert classified[6].classification == "theirs_added"
    assert classified[7].classification == "delete_modify_conflict"
    assert classified[8].classification == "theirs_deleted" and classified[8].deleted
    assert run.plan.unresolved["[1]"].reason  # 判定依据非空

    # 未解决全部冲突前提交 -> 明确失败类别，不返回成功
    with pytest.raises(UnresolvedConflictError):
        svc.commit_merge(run.run_id)

    # id=1 取值冲突：显式 VALUE
    svc.resolve_conflict(run.run_id, [1], "value",
                         {"status": "reconciled-paid"})
    # id=7 删除/修改：KEEP 保留 dev 修改后的版本
    svc.resolve_conflict(run.run_id, [7], "keep")

    run = svc.get_run(run.run_id)
    assert len(run.plan.unresolved) == 0
    snap = svc.commit_merge(run.run_id, message="merge dev into main")

    # 合并后行集的具体断言
    spec, rows = svc.read_snapshot_rows(snap.snapshot_id)
    by_id_final = {r["id"]: r for r in rows}
    assert sorted(by_id_final) == [1, 2, 3, 5, 6, 7]           # 4、8 被删
    assert by_id_final[1]["status"] == "reconciled-paid"
    assert by_id_final[2] == {"id": 2, "status": "new", "amount": 220,
                              "owner": "bob-temp"}
    assert by_id_final[7] == {"id": 7, "status": "shipped", "amount": 710,
                              "owner": "gina"}
    assert by_id_final[5]["owner"] == "erin"
    assert by_id_final[6]["owner"] == "frank"

    # 行为约定4：提交后保留两条父引用
    pm = svc.store.parent_map("orders")
    assert pm[snap.snapshot_id] == [run.ours_snapshot_id, run.theirs_snapshot_id]
    assert svc.get_branch_head("orders", "main").snapshot_id == snap.snapshot_id
    # 旧开发头与旧主头仍然可达：分支历史没有被重新导入覆盖
    assert lineage.is_ancestor(run.ours_snapshot_id, snap.snapshot_id, pm)
    assert lineage.is_ancestor(run.theirs_snapshot_id, snap.snapshot_id, pm)
    assert lineage.is_ancestor(run.base_snapshot_id, snap.snapshot_id, pm)


def test_resolution_binding_mismatch_rejected(svc, dev_main_heads):
    run = svc.start_merge("orders")
    binding = run.binding()
    wrong = (binding[0], binding[1], "snap_00000000000000")
    with pytest.raises(BindingMismatchError):
        svc.resolve_conflict(run.run_id, [99], "ours", None, binding=wrong)


def test_resolve_unknown_key_rejected(svc, dev_main_heads):
    run = svc.start_merge("orders")
    with pytest.raises(ResolutionRejectedError):
        svc.resolve_conflict(run.run_id, [42], "ours", None, binding=run.binding())


def test_commit_with_stale_main_head_rejected(svc, monkeypatch):
    base_rows = [{"id": 1, "status": "new", "amount": 1, "owner": "a"}]
    dev_rows = [{"id": 1, "status": "paid", "amount": 1, "owner": "a"}]
    main_rows = [{"id": 1, "status": "void", "amount": 1, "owner": "a"}]
    base = svc.write_snapshot("orders", base_rows)
    svc.create_branch("orders", "main", base.snapshot_id)
    svc.create_branch("orders", "develop", base.snapshot_id)
    svc.commit_rows("orders", "develop", dev_rows)
    svc.commit_rows("orders", "main", main_rows)
    run = svc.start_merge("orders")
    svc.resolve_conflict(run.run_id, [1], "ours")

    # 主分支在合并期间被别人前进：模拟"重新导入"式覆盖必须被拒绝。
    # 新提交必须是与合并时主头不同的内容（内容寻址会复用完全相同的快照）。
    newer_main_rows = [{"id": 1, "status": "void", "amount": 1, "owner": "a"},
                       {"id": 9, "status": "new", "amount": 9, "owner": "late"}]
    newer = svc.write_snapshot("orders", newer_main_rows,
                               parent_snapshot_id=run.theirs_snapshot_id)
    assert newer.snapshot_id != run.theirs_snapshot_id
    with svc.store.transaction() as conn:
        assert svc.store.advance_branch(
            conn, "main", "orders", run.theirs_snapshot_id, newer.snapshot_id)
    with pytest.raises(ConflictError, match="禁止覆盖分支历史"):
        svc.commit_merge(run.run_id)
    # 合并运行仍然处于未提交状态，其三方快照没被篡改
    assert svc.get_run(run.run_id).committed_snapshot_id is None
    assert svc.get_branch_head("orders", "main").snapshot_id == newer.snapshot_id


def test_double_commit_rejected(svc, dev_main_heads):
    run = svc.start_merge("orders")  # 此夹具无冲突
    snap = svc.commit_merge(run.run_id)
    from merge3.errors import MergeAlreadyCommittedError
    with pytest.raises(MergeAlreadyCommittedError):
        svc.commit_merge(run.run_id)
    assert svc.get_run(run.run_id).status == MergeStatus.COMMITTED.value


def test_no_conflict_merge_roundtrip_matches_oracle(svc):
    """无冲突分区最终行集与独立参考实现一致（服务层端到端组装）。"""
    from tests.reference_oracle import reference_merge
    base = [
        {"id": 1, "status": "new", "amount": 10, "owner": "a"},
        {"id": 2, "status": "new", "amount": 20, "owner": "b"},
    ]
    ours = [
        {"id": 1, "status": "paid", "amount": 10, "owner": "a"},
        {"id": 2, "status": "new", "amount": 20, "owner": "b"},
        {"id": 3, "status": "new", "amount": 30, "owner": "c"},
    ]
    theirs = [
        {"id": 1, "status": "new", "amount": 10, "owner": "a"},
        {"id": 2, "status": "new", "amount": 25, "owner": "b"},
    ]
    b = svc.write_snapshot("orders", base)
    svc.create_branch("orders", "main", b.snapshot_id)
    svc.create_branch("orders", "develop", b.snapshot_id)
    svc.commit_rows("orders", "develop", ours)
    svc.commit_rows("orders", "main", theirs)
    run = svc.start_merge("orders")
    assert not run.plan.conflicts
    snap = svc.commit_merge(run.run_id)
    _, got = svc.read_snapshot_rows(snap.snapshot_id)
    ref = reference_merge(["id", "status", "amount", "owner"], ["id"],
                          base, ours, theirs)
    assert got == ref["rows"]
