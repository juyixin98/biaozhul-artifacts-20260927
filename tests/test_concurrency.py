"""Concurrency tests with real OS threads.

Scenario 1 (disjoint concurrent appends): N threads race from the same base
with N disjoint partitions. Every append must be accepted (rebased as
needed) and the final snapshot must contain exactly N files / N rows.

Scenario 2 (same-partition contention): M threads race to append the SAME
partition. Exactly one wins; every loser must fail with
CONFLICT_OVERLAPPING_PARTITION (never a silent last-writer-wins overwrite).
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.kernel.errors import ErrorCategory


def _row(days: int, region: str, sale_id: int):
    return (sale_id, region, f"2024-01-{days:02d}", sale_id * 10, "x@example.test")


def _src_name(relpath):
    # published relpath: <table>/<request_id>/<idx>-<source-name>
    return Path(relpath).name.split("-", 1)[1]


@pytest.mark.concurrency
def test_concurrent_disjoint_appends_all_merge(service, events_table, make_file):
    n = 8
    regions = [f"r{i}" for i in range(n)]
    files = [
        make_file([_row(1, region, i + 1)], name=f"{region}.parquet")
        for i, region in enumerate(regions)
    ]
    barrier = threading.Barrier(n)
    results: dict[int, object] = {}
    errors: dict[int, Exception] = {}

    def worker(i: int):
        barrier.wait()
        try:
            results[i] = service.commit(
                table_name=events_table,
                operation="APPEND",
                request_id=f"req-conc-{i}",
                source_paths=[files[i]],
                base_snapshot_id=1,  # everyone starts from ROOT
            )
        except Exception as exc:  # noqa: BLE001 - recorded per-thread
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == {}, {i: str(e) for i, e in errors.items()}
    head = service.store.head_snapshot_id(events_table)
    entries = service.store.snapshot_entries(head)
    assert len(entries) == n
    assert {_src_name(e.file_relpath) for e in entries} == {
        f"{r}.parquet" for r in regions
    }
    assert sorted(e.row_count for e in entries) == [1] * n
    # A linear chain of exactly n post-root snapshots.
    snaps = service.store.list_snapshots(events_table)
    assert len(snaps) == n + 1
    committed = [r for r in results.values() if r.status == "COMMITTED"]
    assert len(committed) == n


@pytest.mark.concurrency
def test_concurrent_same_partition_single_winner(service, events_table, make_file):
    m = 5
    files = [
        make_file([_row(1, "us", 100 + i)], name=f"us_{i}.parquet")
        for i in range(m)
    ]
    barrier = threading.Barrier(m)
    outcomes: dict[int, str] = {}
    categories: dict[int, ErrorCategory] = {}

    def worker(i: int):
        barrier.wait()
        try:
            r = service.commit(
                table_name=events_table,
                operation="APPEND",
                request_id=f"req-race-{i}",
                source_paths=[files[i]],
                base_snapshot_id=1,
            )
            outcomes[i] = r.status
        except Exception as exc:  # noqa: BLE001
            outcomes[i] = "ERROR"
            categories[i] = exc.category

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(m)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [i for i, s in outcomes.items() if s == "COMMITTED"]
    losers = [i for i, s in outcomes.items() if s == "ERROR"]
    assert len(winners) == 1, outcomes
    assert len(losers) == m - 1
    assert all(
        categories[i] is ErrorCategory.CONFLICT_OVERLAPPING_PARTITION
        for i in losers
    ), {i: categories[i] for i in losers}

    # Final rowset contains exactly the winner's file — data from losers was
    # never published into a snapshot.
    head = service.store.head_snapshot_id(events_table)
    entries = service.store.snapshot_entries(head)
    assert len(entries) == 1
    assert entries[0].row_count == 1
    assert _src_name(entries[0].file_relpath) == f"us_{winners[0]}.parquet"
