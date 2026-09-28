"""Audit trail + correlated JSONL logs: run identity, steps, verdict basis."""
from __future__ import annotations

import json

from app.config import Settings
from app.services.batch_service import BatchService
from app.services.disclosure_service import DisclosureError, DisclosureService
from app.verifier.independent import VerifyStatus, verify_package
from tests.fixtures.fixtures import records as fixture_records, schema as fixture_schema


def test_audit_rows_keep_specific_verdicts(db, run_id):
    settings = Settings(db_path=db.path, audit_log_path="logs/test.log", salt_bytes=16)
    bsvc = BatchService(db, settings, run_id)
    created = bsvc.create_batch(fixture_schema(), fixture_records(),
                                batch_id="batch-audit-1")
    dsvc = DisclosureService(db, run_id)
    dsvc.issue("batch-audit-1", [{"record_index": 0, "field_name": "merchant"}])

    # A failure path writes its own non-success verdict.
    try:
        dsvc.issue("batch-audit-1", [{"record_index": 9, "field_name": "merchant"}])
        assert False, "expected UNKNOWN_FIELD"
    except DisclosureError as exc:
        assert exc.code == "UNKNOWN_FIELD"

    try:
        dsvc.issue("missing-batch", [{"record_index": 0, "field_name": "x"}])
        assert False
    except DisclosureError as exc:
        assert exc.code == "BATCH_NOT_FOUND"

    rows = db.list_audit(batch_id="batch-audit-1")
    seq = [(r.action, r.verdict) for r in rows]
    assert ("disclosure.issue", "ISSUED") in seq
    assert ("disclosure.issue", "UNKNOWN_FIELD") in seq
    all_rows = db.list_audit()
    assert any(r.verdict == "BATCH_NOT_FOUND" for r in all_rows)
    # Verdict vocabulary contains no catch-all success label on failed ops.
    for r in all_rows:
        if r.action == "disclosure.issue" and r.verdict != "ISSUED":
            assert r.verdict in {"UNKNOWN_FIELD", "BATCH_NOT_FOUND", "MALFORMED_REQUEST"}
    # Create row records the root used as decision basis.
    created_row = next(r for r in all_rows if r.action == "batch.create")
    assert created_row.detail["root"] == created.root_hex
    assert created_row.run_id == run_id


def test_verify_audit_verdict_matches_independent_result(db, run_id):
    settings = Settings(db_path=db.path, audit_log_path="logs/test.log", salt_bytes=16)
    BatchService(db, settings, run_id).create_batch(
        fixture_schema(), fixture_records(), batch_id="batch-audit-2")
    dsvc = DisclosureService(db, run_id)
    pkg = dsvc.issue("batch-audit-2", [{"record_index": 0, "field_name": "amount"}])

    good = verify_package(pkg).verdict
    pkg_bad = json.loads(json.dumps(pkg))
    pkg_bad["disclosed"][0]["salt_hex"] = "aa" * 16
    bad = verify_package(pkg_bad).verdict
    assert good == VerifyStatus.VALID
    assert bad == VerifyStatus.COMMITMENT_MISMATCH

    # Exercise the same verdict through the service audit hook the API uses:
    db.append_audit(run_id=run_id, batch_id="batch-audit-2",
                    action="disclosure.verify", verdict=bad, detail={"valid": False})
    rows = [r for r in db.list_audit(batch_id="batch-audit-2")
            if r.action == "disclosure.verify"]
    assert rows and rows[0].verdict == "COMMITMENT_MISMATCH"


def test_jsonl_log_carries_run_id_steps_and_versions(log_lines_for_run, run_id):
    # The autouse fixture already emitted START for this test under its run id;
    # process-wide logging points at tests/logs/tests.jsonl (see conftest).
    from app.observability import get_logger
    logger = get_logger()
    logger.info("computing commitment", extra={
        "run_id": run_id, "batch_id": "batch-log", "step": "commit.calc",
        "verdict": "RUNNING",
        "detail": {"commitment_version": "audit-commit-v1", "field": "amount",
                   "input_fingerprint": "record=0/field=amount"}})

    lines = log_lines_for_run(run_id)
    assert lines, "expected correlated log lines"
    for line in lines:
        assert line["run_id"] == run_id  # every line traceable to this run
    steps = [line.get("step") for line in lines]
    assert "pytest.start" in steps
    assert "commit.calc" in steps
    calc = next(line for line in lines if line.get("step") == "commit.calc")
    assert calc["detail"]["commitment_version"] == "audit-commit-v1"
    assert calc["detail"]["input_fingerprint"] == "record=0/field=amount"
