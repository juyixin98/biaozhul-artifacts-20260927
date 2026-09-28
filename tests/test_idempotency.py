"""Lost-response / idempotency tests.

The client never got its first response (network), so it retries with the
same request_id and the same payload. The service must return the stored
outcome exactly once — duplicate snapshots/files are not created.
Rejected outcomes replay as the *same* error category.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.kernel.errors import ErrorCategory

ROWS_A = [(1, "us", "2024-01-01", 10, "a@example.test")]
ROWS_B = [(2, "us", "2024-01-02", 20, "b@example.test")]
ROWS_A_V2 = [(3, "us", "2024-01-01", 30, "c@example.test")]


def test_successful_commit_is_replayed_not_recommitted(
    service, events_table, make_file
):
    f = make_file(ROWS_A, name="a.parquet")
    r1 = service.commit(
        table_name=events_table, operation="APPEND", request_id="req-net",
        source_paths=[f], base_snapshot_id=1,
    )
    head_after = service.store.head_snapshot_id(events_table)

    # Response lost: identical retry with same request_id + same file bytes.
    r2 = service.commit(
        table_name=events_table, operation="APPEND", request_id="req-net",
        source_paths=[f], base_snapshot_id=1,
    )
    assert r2.replayed is True
    assert r2.status == "COMMITTED"
    assert r2.snapshot_id == r1.snapshot_id
    assert service.store.head_snapshot_id(events_table) == head_after
    # Exactly one snapshot after root, exactly one committed physical file.
    snaps = service.store.list_snapshots(events_table)
    assert [s["operation"] for s in snaps] == ["ROOT", "APPEND"]
    files = service.storage.warehouse_files(events_table)
    assert len(files) == 1
    commits = service.store.list_commits(events_table)
    assert len(commits) == 1


def test_rejected_commit_replays_same_category(service, events_table, make_file):
    a = make_file(ROWS_A, name="a.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-a",
        source_paths=[a], base_snapshot_id=1,
    )
    other = make_file(
        [(9, "us", "2024-01-01", 99, "z@example.test")], name="other.parquet"
    )
    # First attempt from stale base is rejected...
    with pytest.raises(Exception) as exc1:
        service.commit(
            table_name=events_table, operation="APPEND", request_id="req-stale",
            source_paths=[other], base_snapshot_id=1,
        )
    assert exc1.value.category is ErrorCategory.CONFLICT_OVERLAPPING_PARTITION
    # ...retry (still lost-response, same payload) must replay same category,
    # and not silently succeed after rebasing.
    with pytest.raises(Exception) as exc2:
        service.commit(
            table_name=events_table, operation="APPEND", request_id="req-stale",
            source_paths=[other], base_snapshot_id=1,
        )
    assert exc2.value.category is ErrorCategory.CONFLICT_OVERLAPPING_PARTITION
    assert exc2.value.details.get("replayed") is True


def test_same_request_id_with_different_payload_is_rejected(
    service, events_table, make_file
):
    f1 = make_file(ROWS_A, name="a.parquet")
    service.commit(
        table_name=events_table, operation="APPEND", request_id="req-dup",
        source_paths=[f1], base_snapshot_id=1,
    )
    f2 = make_file(ROWS_B, name="b.parquet")
    with pytest.raises(Exception) as exc:
        service.commit(
            table_name=events_table, operation="APPEND", request_id="req-dup",
            source_paths=[f2], base_snapshot_id=1,
        )
    assert exc.value.category is ErrorCategory.VALIDATION
    assert "different payload" in exc.value.message


def test_unreadable_file_failure_carries_request_id_and_replays_same_category(
    service, events_table, make_file
):
    bad = make_file([], raw_bytes=b"not parquet")
    with pytest.raises(Exception) as exc1:
        service.commit(
            table_name=events_table, operation="APPEND",
            request_id="req-bad-idem", source_paths=[bad], base_snapshot_id=1,
        )
    assert exc1.value.category is ErrorCategory.STAGING_FAILED
    # The caller-supplied request id rides along even on an adapter failure.
    assert exc1.value.request_id == "req-bad-idem"

    # Retrying the same bad input (lost response) replays the same category.
    with pytest.raises(Exception) as exc2:
        service.commit(
            table_name=events_table, operation="APPEND",
            request_id="req-bad-idem", source_paths=[bad], base_snapshot_id=1,
        )
    assert exc2.value.category is ErrorCategory.STAGING_FAILED
    assert exc2.value.details.get("replayed") is True
    # Exactly one terminal commit row, no snapshot created.
    commits = service.store.list_commits(events_table)
    assert len(commits) == 1
    assert commits[0].status == "FAILED"
    assert service.store.head_snapshot_id(events_table) == 1
