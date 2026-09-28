"""Orphan-folder and cleanup-ledger scenarios.

Covers:
- a physical directory/file that belongs to no table is reported and removed
  with a cleanup ledger row per file;
- a file left over from an OVERWRITE (still on disk, unreferenced by the
  current snapshot) is an orphan while remaining referenced by the *old*
  snapshot — and therefore must NOT be reclaimed (time travel);
- staged files from a rejected commit get per-file ledger rows and are gone.
"""
from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq


def _src(relpath):
    return Path(relpath).name.split("-", 1)[1]

ROWS_A = [(1, "us", "2024-01-01", 10, "a@example.test"),
          (2, "us", "2024-01-01", 20, "b@example.test")]
ROWS_A_V2 = [(3, "us", "2024-01-01", 300, "c@example.test")]
ROWS_EU = [(4, "eu", "2024-01-01", 40, "d@example.test")]


def test_unrelated_orphan_directory_detected_and_removed(
    service, container, events_table, make_file
):
    # Plant a directory that looks like a physical table but metadata knows
    # nothing about it.
    wh = container.settings.warehouse_dir
    orphan_table = wh / "_orphan_demo"
    orphan_table.mkdir(parents=True)
    stray = orphan_table / "stray.parquet"
    import pyarrow as pa

    pq.write_table(pa.table({"x": [1, 2, 3]}), stray)

    scan = service.scan_orphans()
    assert any(
        p.endswith("stray.parquet") for p in scan["orphan_files"]
    ), scan
    assert any(
        d == "_orphan_demo" for d in scan["orphan_directories"]
    ), scan

    result = service.reconcile_orphans()
    assert any(p.endswith("stray.parquet") for p in result["removed_files"])
    assert not stray.exists()
    assert not orphan_table.exists()
    # Every removed file got its own ledger record marked REMOVED.
    records = container.ledger.list_records()
    kinds = {(r.kind, r.status, r.path.endswith("stray.parquet")) for r in records}
    assert ("WAREHOUSE_ORPHAN", "REMOVED", True) in kinds


def test_overwritten_file_is_not_orphan_while_old_snapshot_exists(
    service, container, events_table, make_file
):
    a = make_file(ROWS_A, name="a.parquet")
    ra = service.commit(
        table_name=events_table, operation="APPEND", request_id="req-a",
        source_paths=[a], base_snapshot_id=1,
    )
    eu = make_file(ROWS_EU, name="eu.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-eu",
        source_paths=[eu], base_snapshot_id=ra.snapshot_id,
    )
    head_before = service.store.head_snapshot_id(events_table)
    v2 = make_file(ROWS_A_V2, name="v2.parquet")
    ovw = service.commit(
        table_name=events_table, operation="OVERWRITE", request_id="req-ovw",
        source_paths=[v2], base_snapshot_id=head_before,
    )

    # Old a.parquet is still referenced by snapshot 2 -> not an orphan.
    scan = service.scan_orphans()
    assert scan["orphan_files"] == [], scan
    # But the current snapshot only has v2 + eu.
    current = service.store.snapshot_entries(ovw.snapshot_id)
    assert {_src(e.file_relpath) for e in current} == {"v2.parquet", "eu.parquet"}
    # And the old snapshot still exposes a.parquet (immutability).
    old = service.store.snapshot_entries(ra.snapshot_id)
    assert {_src(e.file_relpath) for e in old} == {"a.parquet"}


def test_rejected_commit_staged_files_are_ledgered_and_removed(
    service, container, events_table, make_file
):
    a = make_file(ROWS_A, name="a.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-a",
        source_paths=[a], base_snapshot_id=1,
    )
    stale = make_file(
        [(9, "us", "2024-01-01", 99, "z@example.test")], name="stale.parquet"
    )
    try:
        service.commit(
            table_name=events_table, operation="APPEND", request_id="req-lose",
            source_paths=[stale], base_snapshot_id=1,
        )
    except Exception:
        pass

    # The staged copy of the loser is gone and ledgered per file.
    staged = list(container.settings.staging_dir.glob("*"))
    staged = [p for p in staged if not p.name.startswith(".")]
    assert staged == []
    records = [
        r for r in container.ledger.list_records() if r.request_id == "req-lose"
    ]
    assert len(records) == 1
    assert records[0].status == "REMOVED"
    assert records[0].kind == "STALE_STAGED_FILE"
    # Nothing published for the loser.
    assert all(
        "stale" not in p for p in service.storage.warehouse_files(events_table)
    )
