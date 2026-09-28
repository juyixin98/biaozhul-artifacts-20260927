"""Metadata transaction tests: atomicity and failure persistence."""
from __future__ import annotations

import sqlite3

import pytest

from dictsvc.core import BatchInput, CardinalityOverflow, encode_run
from dictsvc.metadata.store import MetadataStore
from dictsvc.service import RunLogger, Service
from dictsvc.core.errors import RunConflict

from . import fixtures
from .conftest import to_batch_input


def _service(settings):
    return Service(MetadataStore(settings.sqlite_path),
                   RunLogger(settings), settings.version_info())


def test_success_commits_run_batches_entries_atomically(settings):
    svc = _service(settings)
    refs = fixtures.repeated_dict_items()
    out = svc.run([to_batch_input(b) for b in refs],
                  {"run_id": "r1", "target_width": 8,
                   "width_policy": "reject", "on_duplicate_values": "merge"})
    assert out["ok"] is True

    db = sqlite3.connect(settings.sqlite_path)
    try:
        runs = db.execute("SELECT status, cardinality FROM runs").fetchall()
        assert runs == [("ok", 3)]
        n_batches = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        n_entries = db.execute("SELECT COUNT(*) FROM global_entries") \
            .fetchone()[0]
        n_events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert n_batches == 2 and n_entries == 3 and n_events > 3
        # Stored remap is readable back with global indices + bitmap.
        row = db.execute(
            "SELECT global_indices, valid FROM batches "
            "WHERE batch_id='b1'").fetchone()
        import json
        assert json.loads(row[0]) == [0, 1, 0, 0, -1]
        assert json.loads(row[1]) == [True, True, True, True, False]
    finally:
        db.close()


def test_failed_run_persists_without_partial_batches(settings):
    svc = _service(settings)
    with pytest.raises(CardinalityOverflow):
        svc.run([to_batch_input(b) for b in fixtures.overflow_257()],
                {"run_id": "overflow", "target_width": 8,
                 "width_policy": "reject", "on_duplicate_values": "merge"})

    db = sqlite3.connect(settings.sqlite_path)
    db.row_factory = sqlite3.Row
    try:
        run = db.execute("SELECT * FROM runs WHERE run_id='overflow'") \
            .fetchone()
        assert run["status"] == "failed"
        assert run["error_category"] == "CARDINALITY_OVERFLOW"
        # No partial batch/dictionary rows survived the rollback.
        assert db.execute(
            "SELECT COUNT(*) FROM batches WHERE run_id='overflow'") \
            .fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM global_entries WHERE run_id='overflow'") \
            .fetchone()[0] == 0
        # Events leading to the failure were recorded for reproducibility.
        assert db.execute(
            "SELECT COUNT(*) FROM events WHERE run_id='overflow'") \
            .fetchone()[0] >= 1
    finally:
        db.close()


def test_duplicate_runid_does_not_corrupt_first_run(settings):
    svc = _service(settings)
    refs = fixtures.empty_and_nulls()
    opts = {"run_id": "dup", "target_width": 8,
            "width_policy": "reject", "on_duplicate_values": "merge"}
    svc.run([to_batch_input(b) for b in refs], opts)
    with pytest.raises(RunConflict):
        svc.run([to_batch_input(b) for b in refs], opts)

    stored = MetadataStore(settings.sqlite_path).get_run("dup")
    assert stored["status"] == "ok"
    assert {b["batch_id"] for b in stored["batches"]} == {"empty",
                                                          "all_null"}


def test_store_rejects_duplicate_runid_atomically(tmp_path):
    store = MetadataStore(str(tmp_path / "m.db"))
    enc = encode_run([BatchInput("b", ("a",), (0,), (True,))])
    store.save_success("x", enc, [], )
    with pytest.raises(RunConflict):
        store.save_success("x", enc, [])
    # Exactly one run, one batch: the second txn fully rolled back.
    db = sqlite3.connect(str(tmp_path / "m.db"))
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM batches WHERE run_id='x'").fetchone()[0] == 1
    finally:
        db.close()
