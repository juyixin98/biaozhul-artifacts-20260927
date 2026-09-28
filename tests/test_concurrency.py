"""并发与冲突测试：真实线程并发提交，断言接受集合、失败类别与最终快照行集。"""

from __future__ import annotations

import threading

import pytest

from lake_txn import errors
from tests.conftest import TABLE, stage_inline


def _run_threads(target, n):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_disjoint_appends_all_accepted_via_merge(service, oracle):
    partitions = ["cn", "us", "eu", "jp", "sg"]
    results: dict[int, dict] = {}
    barrier = threading.Barrier(len(partitions))

    def worker(i):
        part = partitions[i]
        rid = f"r-{part}"
        stage_inline(service, rid, part, [100 + i])
        barrier.wait()  # 尽量让多个线程同时进入提交
        try:
            results[i] = {"ok": True, **service.commit(TABLE, rid, "APPEND", 0, [f"{rid}-0"])}
        except errors.DomainError as exc:
            results[i] = {"ok": False, "reason_code": exc.reason_code, "detail": exc.detail}

    _run_threads(worker, len(partitions))

    accepted = {partitions[i]: r for i, r in results.items() if r["ok"]}
    rejected = {partitions[i]: r for i, r in results.items() if not r["ok"]}
    assert set(accepted) == set(partitions), rejected
    # 除首个外，其余都应以合并方式接受，且父快照为当时表头（不是 base=0）
    merged = [r["snapshot_id"] for r in accepted.values() if r["merged"]]
    assert len(merged) == len(partitions) - 1

    head = oracle.head_snapshot(TABLE)
    assert head == len(partitions)
    rows = oracle.rows_by_partition(TABLE, head)
    assert set(rows) == set(partitions)
    for i, part in enumerate(partitions):
        assert [r["order_id"] for r in rows[part]] == [100 + i]

    log = {r["request_id"]: r for r in oracle.commit_log()}
    assert all(r["status"] == "ACCEPTED" for r in log.values())
    # 每个快照都可独立读取，链完整（parent 依次相接）
    chain = {s["id"]: s for s in oracle.snapshot_chain(TABLE)}
    assert chain[1]["parent_id"] == 0
    for sid in range(2, head + 1):
        assert chain[sid]["parent_id"] == sid - 1


def test_concurrent_same_partition_appends_conflict_then_retries_succeed(service, oracle):
    n = 6
    barrier = threading.Barrier(n)
    outcomes: dict[int, dict] = {}

    def worker(i):
        rid = f"r-{i}"
        stage_inline(service, rid, "cn", [i])
        barrier.wait()
        try:
            outcomes[i] = {"ok": True, **service.commit(TABLE, rid, "APPEND", 0, [f"{rid}-0"])}
        except errors.DomainError as exc:
            outcomes[i] = {"ok": False, "reason_code": exc.reason_code}

    _run_threads(worker, n)

    accepted = [i for i, o in outcomes.items() if o["ok"]]
    rejected = [(i, o) for i, o in outcomes.items() if not o["ok"]]
    assert len(accepted) == 1
    assert all(o["reason_code"] == errors.PARTITION_CONFLICT for _, o in rejected)

    # 冲突方按声明规则刷新基线后重试：每个都能追加成功（顺序追加，互不丢数据）
    for i, _ in rejected:
        rid = f"r-{i}-retry"
        stage_inline(service, rid, "cn", [i])
        base = oracle.head_snapshot(TABLE)
        res = service.commit(TABLE, rid, "APPEND", base, [f"{rid}-0"])
        assert res["status"] == "ACCEPTED"

    rows = oracle.rows_of(TABLE, oracle.head_snapshot(TABLE))
    assert sorted(r["order_id"] for r in rows) == sorted(range(n))


def test_concurrent_same_partition_overwrite_single_winner(service, oracle):
    # 先放一个基线快照包含 cn/us
    stage_inline(service, "base-cn", "cn", [1])
    service.commit(TABLE, "base-cn", "APPEND", 0, ["base-cn-0"])
    stage_inline(service, "base-us", "us", [2])
    service.commit(TABLE, "base-us", "APPEND", 1, ["base-us-0"])

    n = 4
    barrier = threading.Barrier(n)
    outcomes: dict[int, dict] = {}

    def worker(i):
        rid = f"ow-{i}"
        stage_inline(service, rid, "cn", [1000 + i])
        barrier.wait()
        try:
            outcomes[i] = {"ok": True, **service.commit(
                TABLE, rid, "OVERWRITE", 2, [f"{rid}-0"], drop_partitions=["cn"]
            )}
        except errors.DomainError as exc:
            outcomes[i] = {"ok": False, "reason_code": exc.reason_code}

    _run_threads(worker, n)

    winners = [i for i, o in outcomes.items() if o["ok"]]
    losers = [(i, o) for i, o in outcomes.items() if not o["ok"]]
    assert len(winners) == 1, outcomes
    # 重叠分区覆盖：输家必须拿到冲突，绝不允许最后写获胜
    reason_codes = {o["reason_code"] for _, o in losers}
    assert reason_codes <= {errors.CONCURRENT_OVERWRITE, errors.PARTITION_CONFLICT}
    assert errors.CONCURRENT_OVERWRITE in reason_codes  # 与并发覆盖相撞

    head = oracle.head_snapshot(TABLE)
    rows = oracle.rows_by_partition(TABLE, head)
    # 只有赢家的那一行覆盖了 cn
    assert [r["order_id"] for r in rows["cn"]] == [1000 + winners[0]]
    # us 从未被任何覆盖触及
    assert [r["order_id"] for r in rows["us"]] == [2]

    # commit_log 明确记录每个输家的 REJECTED 与原因
    log = {r["request_id"]: r for r in oracle.commit_log()}
    for i, o in losers:
        assert log[f"ow-{i}"]["status"] == "REJECTED"
        assert log[f"ow-{i}"]["reason_code"] == o["reason_code"]
        assert log[f"ow-{i}"]["snapshot_id"] is None


def test_stale_overwrite_disjoint_from_append_is_rejected_stale_not_last_writer(service):
    stage_inline(service, "a", "cn", [1])
    service.commit(TABLE, "a", "APPEND", 0, ["a-0"])  # snap1
    stage_inline(service, "b", "us", [2])
    service.commit(TABLE, "b", "APPEND", 1, ["b-0"])  # snap2，表头

    # 客户端基线停在 1，试图覆盖 eu（与并发 append us 不相交）：不自动重放
    stage_inline(service, "ow", "eu", [9])
    with pytest.raises(errors.DomainError) as ei:
        service.commit(TABLE, "ow", "OVERWRITE", 1, ["ow-0"], drop_partitions=["eu"])
    assert ei.value.reason_code == errors.STALE_BASE_OVERWRITE
    assert ei.value.detail["head_snapshot_id"] == 2


def test_disjoint_append_merges_with_concurrent_overwrite(service, oracle):
    stage_inline(service, "a", "cn", [1])
    service.commit(TABLE, "a", "APPEND", 0, ["a-0"])  # snap1 cn
    stage_inline(service, "ow", "cn", [10])
    service.commit(TABLE, "ow", "OVERWRITE", 1, ["ow-0"], drop_partitions=["cn"])  # snap2

    # 基线 1 的 eu 追加，与对 cn 的覆盖不相交 -> 合并，父快照为 2
    stage_inline(service, "c", "eu", [3])
    res = service.commit(TABLE, "c", "APPEND", 1, ["c-0"])
    assert res["status"] == "ACCEPTED"
    assert res["merged"] is True
    assert res["parent_snapshot_id"] == 2

    rows = oracle.rows_by_partition(TABLE, 3)
    assert [r["order_id"] for r in rows["cn"]] == [10]
    assert [r["order_id"] for r in rows["eu"]] == [3]
