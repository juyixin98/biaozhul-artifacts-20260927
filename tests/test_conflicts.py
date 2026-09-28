"""Metadata transaction semantics: snapshot rowsets and conflict categories.

These tests assert *concrete* outcomes:
- exact per-snapshot file sets and row counts;
- exact error categories (not just "a 4xx happened");
- stale-snapshot diagnostics including base/head and overlapping partitions.
Expected row counts are literal constants derived from authored rows.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.kernel.errors import ErrorCategory

US_0101 = [(1, "us", "2024-01-01", 10, "a@example.test"),
           (2, "us", "2024-01-01", 20, "b@example.test")]
US_0102 = [(3, "us", "2024-01-02", 30, "c@example.test")]
EU_0101 = [(4, "eu", "2024-01-01", 40, "d@example.test")]
US_0101_V2 = [(101, "us", "2024-01-01", 100, "e@example.test"),
              (102, "us", "2024-01-01", 200, "f@example.test")]
EU_0101_V2 = [(201, "eu", "2024-01-01", 400, "g@example.test")]


def _src_name(relpath):
    # published relpath: <table>/<request_id>/<idx>-<source-name>
    return Path(relpath).name.split("-", 1)[1]


def _rows(service, table, snapshot_id):
    return service.store.snapshot_entries(snapshot_id)


def _row_count(service, table, snapshot_id):
    return sum(e.row_count for e in _rows(service, table, snapshot_id))


def _partition_files(service, table, snapshot_id, key):
    return {
        _src_name(e.file_relpath)
        for e in _rows(service, table, snapshot_id)
        if key in e.partition_keys
    }



def test_append_on_current_head_creates_incremental_snapshot(
    service, events_table, make_file
):
    f1 = make_file(US_0101, name="us0101.parquet")
    r1 = service.commit(
        table_name=events_table, operation="APPEND",
        request_id="req-1", source_paths=[f1], base_snapshot_id=1,
    )
    assert r1.status == "COMMITTED"
    assert r1.snapshot_id == 2
    assert r1.rebased is False
    assert _row_count(service, events_table, 2) == 2

    # Root snapshot is still empty; history is append-only.
    assert _row_count(service, events_table, 1) == 0

    f2 = make_file(US_0102, name="us0102.parquet")
    r2 = service.commit(
        table_name=events_table, operation="APPEND",
        request_id="req-2", source_paths=[f2], base_snapshot_id=2,
    )
    assert r2.snapshot_id == 3
    # Snapshot 3 rowset = materialised union = 3 rows, 2 files.
    assert _row_count(service, events_table, 3) == 3
    assert len(_rows(service, events_table, 3)) == 2


def test_disjoint_partition_append_on_stale_base_is_rebased_and_accepted(
    service, events_table, make_file
):
    # Client A commits us/0101 from base=1 -> snapshot 2.
    a = make_file(US_0101, name="a.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-a",
        source_paths=[a], base_snapshot_id=1,
    )
    # Client B still holds base=1 but appends the disjoint partition eu/0101.
    b = make_file(EU_0101, name="b.parquet")
    rb = service.commit(
        table_name=events_table, operation="APPEND", request_id="req-b",
        source_paths=[b], base_snapshot_id=1,
    )
    assert rb.status == "COMMITTED"
    assert rb.rebased is True  # declared merge rule fired
    assert rb.snapshot_id == 3
    entries = _rows(service, events_table, 3)
    assert {_src_name(e.file_relpath) for e in entries} == {"a.parquet", "b.parquet"}
    assert sum(e.row_count for e in entries) == 3
    # New snapshot's parent is the intervening snapshot 2.
    snap = service.store.get_snapshot(events_table, 3)
    assert snap["parent_snapshot_id"] == 2


def test_same_partition_append_on_stale_base_is_hard_conflict(
    service, events_table, make_file
):
    a = make_file(US_0101, name="a.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-a",
        source_paths=[a], base_snapshot_id=1,
    )
    # B also touches us/day=2024-01-01 from the stale base: must NOT merge.
    b = make_file(
        [(9, "us", "2024-01-01", 99, "z@example.test")], name="b.parquet"
    )
    with pytest.raises(Exception) as exc:
        service.commit(
            table_name=events_table, operation="APPEND", request_id="req-b",
            source_paths=[b], base_snapshot_id=1,
        )
    err = exc.value
    assert err.category is ErrorCategory.CONFLICT_OVERLAPPING_PARTITION
    assert err.details["base_snapshot_id"] == 1
    assert err.details["head_snapshot_id"] == 2
    assert "region=us/day=2024-01-01" in err.details["overlapping_partitions"]

    # No new snapshot; HEAD unchanged (not last-writer-wins).
    assert service.store.head_snapshot_id(events_table) == 2
    assert _row_count(service, events_table, 2) == 2


def test_overlapping_overwrite_is_detected_even_when_files_are_newer(
    service, events_table, make_file
):
    # A and B both plan an overwrite of us/0101 from base=1.
    a = make_file(US_0101, name="a.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-a",
        source_paths=[a], base_snapshot_id=1,
    )
    v2 = make_file(US_0101_V2, name="v2.parquet")
    with pytest.raises(Exception) as exc:
        service.commit(
            table_name=events_table, operation="OVERWRITE",
            request_id="req-ovw", source_paths=[v2], base_snapshot_id=1,
        )
    assert exc.value.category is ErrorCategory.CONFLICT_OVERLAPPING_PARTITION
    assert service.store.head_snapshot_id(events_table) == 2


def test_overwrite_replaces_exactly_target_partition(
    service, events_table, make_file
):
    for rid, rows, name, base in [
        ("req-a", US_0101, "a.parquet", 1),
        ("req-b", EU_0101, "b.parquet", 2),
        ("req-c", US_0102, "c.parquet", 3),
    ]:
        f = make_file(rows, name=name)
        service.commit(
            table_name=events_table, operation="APPEND", request_id=rid,
            source_paths=[f], base_snapshot_id=base,
        )
    assert _row_count(service, events_table, 4) == 4

    v2 = make_file(US_0101_V2, name="v2.parquet")
    r = service.commit(
        table_name=events_table, operation="OVERWRITE", request_id="req-ovw",
        source_paths=[v2], base_snapshot_id=4,
    )
    assert r.status == "COMMITTED"
    entries = _rows(service, events_table, r.snapshot_id)
    # Old us/0101 file removed; other partitions untouched.
    names = {_src_name(e.file_relpath) for e in entries}
    assert names == {"b.parquet", "c.parquet", "v2.parquet"}
    assert sum(e.row_count for e in entries) == 2 + 1 + 1
    assert _partition_files(
        service, events_table, r.snapshot_id, "region=us/day=2024-01-01"
    ) == {"v2.parquet"}
    # The old file remains readable at the PREVIOUS snapshot (immutability,
    # time travel rowset).
    assert _partition_files(
        service, events_table, 4, "region=us/day=2024-01-01"
    ) == {"a.parquet"}


def test_stale_overwrite_on_unchanged_target_partition_is_rebased(
    service, events_table, make_file
):
    a = make_file(US_0101, name="a.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-a",
        source_paths=[a], base_snapshot_id=1,
    )
    b = make_file(EU_0101, name="b.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-b",
        source_paths=[b], base_snapshot_id=2,
    )
    # Client overwrites us/0101 from base=2 while HEAD=3 (eu changed only).
    v2 = make_file(US_0101_V2, name="v2.parquet")
    r = service.commit(
        table_name=events_table, operation="OVERWRITE", request_id="req-ovw",
        source_paths=[v2], base_snapshot_id=2,
    )
    assert r.status == "COMMITTED"
    assert r.rebased is True
    entries = _rows(service, events_table, r.snapshot_id)
    assert {_src_name(e.file_relpath) for e in entries} == {"b.parquet", "v2.parquet"}


def test_overwrite_file_spanning_two_partitions_is_rejected(service, events_table, make_file):
    f = make_file(US_0101 + US_0102, name="multi.parquet")
    with pytest.raises(Exception) as exc:
        service.commit(
            table_name=events_table, operation="OVERWRITE",
            request_id="req-bad", source_paths=[f], base_snapshot_id=1,
        )
    assert exc.value.category is ErrorCategory.VALIDATION
    assert service.store.head_snapshot_id(events_table) == 1


def test_schema_drift_is_validation_error_not_a_snapshot(service, events_table, make_file):
    import pyarrow as pa

    good = make_file(US_0101, name="good.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-a",
        source_paths=[good], base_snapshot_id=1,
    )
    drifted = make_file(
        [(1, "us", "2024-01-01", "lots", "x@example.test")],
        name="drift.parquet",
        schema=pa.schema(
            [
                ("sale_id", pa.int64()),
                ("region", pa.string()),
                ("day", pa.string()),
                ("amount", pa.string()),
                ("owner", pa.string()),
            ]
        ),
    )
    with pytest.raises(Exception) as exc:
        service.commit(
            table_name=events_table, operation="APPEND", request_id="req-bad",
            source_paths=[drifted], base_snapshot_id=2,
        )
    assert exc.value.category is ErrorCategory.VALIDATION
    assert service.store.head_snapshot_id(events_table) == 2
