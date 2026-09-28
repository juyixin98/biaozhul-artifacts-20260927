"""Audit hash-chain, per-run log correlation and forensic retention tests."""
from __future__ import annotations

import json

from tests.fixtures import builder as fb


def test_audit_chain_verifies_for_accept_and_reject(service, audit, settings):
    service.extract(fb.zip_bytes([{"name": "a.txt", "data": b"ok"}]), "good.zip")
    service.extract(fb.zip_bytes([{"name": "../bad", "data": b"x"}]), "bad.zip")

    report = audit.verify_chain()
    assert report["ok"] is True, report
    assert report["checked"] >= 2  # at least one event per run, usually more


def test_chain_detects_tampering(service, audit, settings):
    result = service.extract(fb.zip_bytes([{"name": "../bad", "data": b"x"}]), "b.zip")
    run_id = result.run_id
    # Tamper directly with an audit row, bypassing the writer.
    with audit._conn:
        audit._conn.execute(
            "UPDATE events SET message='forged success' WHERE run_id=?", (run_id,)
        )
    report = audit.verify_chain(run_id)
    assert report["ok"] is False
    assert report["reason"] == "event_hash HMAC mismatch"


def test_run_log_correlates_identity_input_and_version(service, settings):
    data = fb.zip_bytes([{"name": "../x", "data": b"z"}])
    result = service.extract(data, "trace.zip")
    run_id = result.run_id

    lines = [json.loads(l) for l in result.workspace.log_path.read_text().splitlines()]
    assert lines, "run log must not be empty"
    # Every line carries the run id and service version.
    assert all(l["run_id"] == run_id for l in lines)
    assert all(l["version"] == settings.version for l in lines)
    # Input correlation: sha256 present on each line.
    assert all(l["input_sha256"] and len(l["input_sha256"]) == 64 for l in lines)
    # Progress and the judgment basis are both visible.
    stages = [(l["stage"], l["outcome"]) for l in lines]
    assert ("receive", "progress") in stages
    reject = [l for l in lines if l["outcome"] == "rejected"]
    assert reject, "must log a rejected event"
    assert reject[-1]["category"] == "path_escape"
    assert reject[-1]["evidence"]  # offending name recorded


def test_rejected_run_retains_forensic_input_and_log(service, settings):
    data = fb.zip_bytes([{"name": "../x", "data": b"z"}])
    result = service.extract(data, "forensic.zip")
    # No output, but the run directory with input + log is retained for review.
    assert not result.workspace.output_dir.exists()
    assert result.workspace.root.exists()
    assert result.workspace.log_path.exists()
    assert any(result.workspace.input_dir.iterdir())


def test_audit_run_row_records_verdict_and_hash(service, audit, settings):
    import hashlib

    data = fb.zip_bytes([{"name": "a.txt", "data": b"abc"}])
    result = service.extract(data, "r.zip")
    row = audit.get_run(result.run_id)
    assert row["verdict"] == "extracted"
    assert row["input_sha256"] == hashlib.sha256(data).hexdigest()
    assert row["input_size"] == len(data)
    assert row["container"] == "zip"

    rejected = service.extract(fb.zip_bytes([{"name": "../a", "data": b"x"}]), "r2.zip")
    rrow = audit.get_run(rejected.run_id)
    assert rrow["verdict"] == "rejected"
    assert rrow["category"] == "path_escape"


def test_events_show_decision_steps(service, audit, settings):
    result = service.extract(fb.zip_bytes([{"name": "a.txt", "data": b"q"}]), "s.zip")
    events = audit.get_events(result.run_id)
    stages = [e["stage"] for e in events]
    # Processing pipeline is recorded in order.
    assert "receive" in stages
    assert "parse" in stages
    assert "canonical_plan" in stages
    assert "extract_complete" in stages
    # Hash chain links consecutive events.
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["event_hash"]
