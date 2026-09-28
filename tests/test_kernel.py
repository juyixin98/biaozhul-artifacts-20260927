"""执行内核（纯逻辑）穷举测试：只断言具体裁决码、重叠分区与清单结果。"""

from __future__ import annotations

from lake_txn import errors
from lake_txn.kernel import (
    CommitIntent,
    CommitKind,
    CompetingCommit,
    ManifestFile,
    Outcome,
    adjudicate,
    plan_manifest,
)


def intent(kind, base, add, drop=frozenset(), table="t", request_id="r"):
    return CommitIntent(
        table=table,
        request_id=request_id,
        kind=kind,
        base_snapshot_id=base,
        add_partitions=frozenset(add),
        drop_partitions=frozenset(drop),
    )


def other(rid, kind, parts):
    return CompetingCommit(rid, kind, frozenset(parts))


APPEND = CommitKind.APPEND
OVERWRITE = CommitKind.OVERWRITE


# ---------- adjudicate ----------
def test_fresh_base_append_accepted():
    d = adjudicate(intent(APPEND, 5, ["cn"]), 5, ())
    assert d.outcome is Outcome.ACCEPTED
    assert d.reason_code is None
    assert not d.merged


def test_base_in_future_rejected():
    d = adjudicate(intent(APPEND, 9, ["cn"]), 5, ())
    assert d.outcome is Outcome.REJECTED
    assert d.reason_code == errors.BASE_IN_FUTURE


def test_disjoint_append_against_appends_merges():
    d = adjudicate(
        intent(APPEND, 1, ["eu"], request_id="mine"),
        3,
        (other("a", APPEND, ["cn"]), other("b", APPEND, ["us"])),
    )
    assert d.outcome is Outcome.ACCEPTED_MERGE
    assert d.merged is True
    assert d.concurrent_request_ids == ("a", "b")


def test_overlapping_append_against_appends_conflicts_with_exact_partitions():
    d = adjudicate(
        intent(APPEND, 1, ["cn", "eu"]),
        2,
        (other("a", APPEND, ["cn", "us"]),),
    )
    assert d.outcome is Outcome.REJECTED
    assert d.reason_code == errors.PARTITION_CONFLICT
    assert d.conflict_partitions == frozenset({"cn"})  # eu/us 不算冲突


def test_disjoint_append_against_overwrite_merges():
    d = adjudicate(
        intent(APPEND, 1, ["eu"]),
        2,
        (other("ow", OVERWRITE, ["cn"]),),
    )
    assert d.outcome is Outcome.ACCEPTED_MERGE
    assert d.reason_code is None


def test_append_into_overwritten_partition_is_concurrent_overwrite():
    d = adjudicate(
        intent(APPEND, 1, ["cn"]),
        2,
        (other("ow", OVERWRITE, ["cn", "us"]),),
    )
    assert d.outcome is Outcome.REJECTED
    assert d.reason_code == errors.CONCURRENT_OVERWRITE
    assert d.conflict_partitions == frozenset({"cn"})


def test_overwrite_fresh_base_accepted_even_with_intent_scope():
    d = adjudicate(intent(OVERWRITE, 5, ["cn"], frozenset(["cn"])), 5, ())
    assert d.outcome is Outcome.ACCEPTED
    assert not d.merged


def test_overwrite_vs_concurrent_overwrite_conflicts_on_overlap():
    d = adjudicate(
        intent(OVERWRITE, 5, ["cn"], frozenset(["cn"]), request_id="mine"),
        6,
        (other("other-ow", OVERWRITE, ["cn"]),),
    )
    assert d.outcome is Outcome.REJECTED
    assert d.reason_code == errors.CONCURRENT_OVERWRITE
    assert d.conflict_partitions == frozenset({"cn"})
    assert d.concurrent_request_ids == ("other-ow",)


def test_overwrite_vs_concurrent_overwrite_disjoint_still_conflicts():
    # 简化规则：两个 OVERWRITE 互不相交也不自动重放（无法安全合并覆盖语义）
    d = adjudicate(
        intent(OVERWRITE, 5, ["cn"], frozenset(["cn"])),
        6,
        (other("other-ow", OVERWRITE, ["eu"]),),
    )
    assert d.outcome is Outcome.REJECTED
    assert d.reason_code == errors.CONCURRENT_OVERWRITE
    assert d.conflict_partitions == frozenset()


def test_overwrite_vs_mixed_disjoint_history_is_concurrent_overwrite():
    # 并发历史含 OVERWRITE（即使与本次 us 不相交），覆盖一律不自动重放
    d = adjudicate(
        intent(OVERWRITE, 1, ["us"], frozenset(["us"])),
        4,
        (
            other("a", APPEND, ["eu"]),
            other("b", APPEND, ["jp"]),
            other("ow", OVERWRITE, ["cn"]),
        ),
    )
    assert d.outcome is Outcome.REJECTED
    assert d.reason_code == errors.CONCURRENT_OVERWRITE
    assert d.conflict_partitions == frozenset()


def test_overwrite_vs_concurrent_append_overlap_partition_conflict():
    d = adjudicate(
        intent(OVERWRITE, 1, ["cn"], frozenset(["cn"])),
        2,
        (other("a", APPEND, ["cn"]),),
    )
    assert d.outcome is Outcome.REJECTED
    assert d.reason_code == errors.PARTITION_CONFLICT
    assert d.conflict_partitions == frozenset({"cn"})


def test_overwrite_vs_disjoint_concurrent_append_is_stale_base():
    d = adjudicate(
        intent(OVERWRITE, 1, ["cn"], frozenset(["cn"])),
        2,
        (other("a", APPEND, ["eu"]),),
    )
    assert d.outcome is Outcome.REJECTED
    assert d.reason_code == errors.STALE_BASE_OVERWRITE
    assert d.concurrent_request_ids == ("a",)


def test_append_vs_mixed_concurrent_history_uses_exact_overlap():
    # 历史上既有对 eu 的 OVERWRITE，又有对 cn 的 APPEND；本提交追加 us，应合并
    d = adjudicate(
        intent(APPEND, 1, ["us"]),
        3,
        (other("ow", OVERWRITE, ["eu"]), other("ap", APPEND, ["cn"])),
    )
    assert d.outcome is Outcome.ACCEPTED_MERGE


# ---------- plan_manifest ----------
def mf(path, part, rows=1):
    # 指纹按内容区分：路径不同则指纹不同（模拟内容寻址）
    return ManifestFile(path=path, partition=part, sha256=f"sha-{path}", size_bytes=1, row_count=rows)


def test_append_manifest_keeps_all_and_adds_new_dedup_by_path():
    head = (mf("a", "cn"), mf("b", "us"))
    planned = plan_manifest(APPEND, head, (mf("c", "eu"), mf("a", "cn")), frozenset())
    assert {f.path for f in planned} == {"a", "b", "c"}  # 重复路径不产生两个条目


def test_overwrite_manifest_drops_target_partition_and_keeps_others():
    head = (mf("cn-old", "cn"), mf("us-1", "us"), mf("cn-old2", "cn"))
    planned = plan_manifest(
        OVERWRITE, head, (mf("cn-new", "cn"),), frozenset(["cn"])
    )
    assert {f.path for f in planned} == {"us-1", "cn-new"}
    assert [f.partition for f in planned if f.path == "cn-new"] == ["cn"]


def test_overwrite_multiple_partitions():
    head = (mf("cn1", "cn"), mf("us1", "us"), mf("eu1", "eu"))
    planned = plan_manifest(
        OVERWRITE, head, (mf("cn2", "cn"), mf("us2", "us")), frozenset({"cn", "us"})
    )
    assert {f.path for f in planned} == {"cn2", "us2", "eu1"}
