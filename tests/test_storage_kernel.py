"""Execution-kernel tests: atomic staging/publish and the cleanup ledger."""
from __future__ import annotations

from pathlib import Path

from app.kernel.storage import (
    CLEANUP_KIND_PARTIAL_PUBLISH,
    STATUS_PENDING,
    STATUS_REMOVED,
    CleanupLedger,
    FileStorage,
    remove_path,
)
from app.services.commits import _StagedView  # internal view used by service


def _staged(storage: FileStorage, tmp_path: Path, name: str, body: bytes):
    src = tmp_path / name
    src.write_bytes(body)
    return storage.stage(src, "req-test", "deadbeefcafe")


def test_stage_writes_complete_file_before_publish(tmp_path):
    storage = FileStorage(tmp_path / "wh", tmp_path / "stg")
    staged = _staged(storage, tmp_path, "a.parquet", b"PAR1" + b"x" * 100)
    assert staged.staged_path.read_bytes() == b"PAR1" + b"x" * 100
    # No partial temp files linger after staging.
    leftovers = [p for p in storage.staging_dir.iterdir() if p.name.startswith(".writing-")]
    assert leftovers == []


def test_publish_is_immutable_and_atomic(tmp_path):
    storage = FileStorage(tmp_path / "wh", tmp_path / "stg")
    staged = _staged(storage, tmp_path, "a.parquet", b"hello")
    view = _StagedView(staged.staged_path, "req-test", "a.parquet", "deadbeefcafe")
    published = storage.publish_one(view, "events/abc.parquet")
    dest = storage.warehouse_dir / published.published_relpath
    assert dest.read_bytes() == b"hello"
    # Staged file is gone: the move is a rename, never a copy into snapshots.
    assert not staged.staged_path.exists()


def test_each_failed_file_gets_its_own_ledger_record(tmp_path):
    ledger = CleanupLedger(tmp_path / "cleanup.sqlite3")
    paths = []
    for i in range(3):
        p = tmp_path / f"orphan_{i}.parquet"
        p.write_bytes(b"data")
        rec = ledger.record_pending(
            request_id="req-x",
            table_name="events",
            kind=CLEANUP_KIND_PARTIAL_PUBLISH,
            path=p,
        )
        paths.append((p, rec))

    pending = ledger.list_records(STATUS_PENDING)
    assert len(pending) == 3  # one independent row per file

    ok = [remove_path(p, ledger, rec) for p, rec in paths]
    assert ok == [True, True, True]
    removed = ledger.list_records(STATUS_REMOVED)
    assert len(removed) == 3
    assert all(r.error is None for r in removed)
    assert not any(p.exists() for p, _ in paths)


def test_ledger_is_independent_sqlite_file(tmp_path):
    # The cleanup ledger must survive independently of metadata DB state.
    ledger_path = tmp_path / "cleanup.sqlite3"
    ledger = CleanupLedger(ledger_path)
    ledger.record_pending(
        request_id="req-y", table_name="t",
        kind=CLEANUP_KIND_PARTIAL_PUBLISH, path=tmp_path / "z",
    )
    ledger.close()
    reopened = CleanupLedger(ledger_path)
    assert len(reopened.list_records()) == 1
    reopened.close()
