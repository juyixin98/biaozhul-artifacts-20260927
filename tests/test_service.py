"""Service/store/audit tests: state isolation, signatures and tamper detection."""

from __future__ import annotations

import json

import pytest

from osdiff.config import Config
from osdiff.service import Service, ServiceError
from osdiff.types import Failure

from . import fixtures as fx


@pytest.fixture()
def service(tmp_path):
    cfg = Config(data_dir=str(tmp_path), db_path=str(tmp_path / "db.sqlite3"),
                 key_path=str(tmp_path / "k.pem"))
    with Service(cfg) as svc:
        yield svc


def test_run_is_persisted_and_reloadable_with_isolated_witness_rows(service):
    result = service.run_diff(fx.EMPTY, fx.PHOTO_V2_EXPAND)
    run_id = result.run_id

    # Witness rows are scoped to this run id.
    witnesses = service.get_witnesses(run_id)
    assert witnesses and all(w["run_id"] == run_id for w in witnesses)
    assert service.get_witnesses(run_id, category="EXPANSION_PROVEN")
    assert not service.get_witnesses(run_id, category="CONTRACTION")

    # A second, independent run cannot see the first run's witnesses.
    other = service.run_diff(fx.TIGHTEN_V1, fx.TIGHTEN_V2)
    assert service.get_witnesses(other.run_id, category="EXPANSION_PROVEN") == []
    assert service.get_witnesses(run_id, category="EXPANSION_PROVEN")  # first untouched


def test_failed_run_is_recorded_with_failure_class(service):
    with pytest.raises(ServiceError) as ei:
        service.run_diff(fx.BAD_GLOB, fx.EMPTY)
    assert ei.value.code is Failure.PARSE_ERROR

    failed = [r for r in service.list_runs() if r["status"] == "failed"]
    assert len(failed) == 1
    stored = service.get_run(failed[0]["run_id"])
    assert stored["status"] == "failed"
    assert stored["failure"]["code"] == "PARSE_ERROR"
    assert stored["failure"]["details"]["errors"]  # precise locations present


def test_verify_request_rejudges_and_reports_mismatch(service):
    request = {"principal": "bob", "action": "s3:GetObject", "resource": "photos/x",
               "attributes": {}}
    ok = service.verify_request(fx.PHOTO_V2_EXPAND, request, expected_verdict="ALLOW")
    assert ok["consistent"] is True and ok["verdict"] == "ALLOW"

    bad = service.verify_request(fx.PHOTO_V2_EXPAND, request, expected_verdict="DENY_NO_MATCH")
    assert bad["consistent"] is False


def test_verify_request_unknown_value_is_reported_not_allowed(service):
    request = {"principal": "alice", "action": "s3:GetObject", "resource": "docs/x",
               "attributes": {"department": {"__unknown__": True}}}
    out = service.verify_request(fx.NEG_V2, request)
    assert out["verdict"] == "UNKNOWN"
    # UNKNOWN is explicitly framed as "not proven to allow"
    assert "NOT to allow" in out["reason"] or "UNKNOWN" in out["verdict"]


def test_audit_chain_correlates_run_and_request_and_verifies(service):
    r = service.run_diff(fx.EMPTY, fx.PHOTO_V2_EXPAND)
    events = service.get_audit(run_id=r.run_id)
    stages = [e["stage"] for e in events]
    assert "diff-requested" in stages and "space-built" in stages and "run-stored" in stages
    assert all(e["run_id"] == r.run_id for e in events)
    assert all(e["caller_location"] for e in events)  # explainability: where it happened

    verdict = service.verify_audit_chain()
    assert verdict["ok"] is True and verdict["events_verified"] == len(events)


def test_tampering_with_an_audit_row_breaks_the_chain(service):
    r = service.run_diff(fx.EMPTY, fx.PHOTO_V2_EXPAND)
    conn = service.store.conn
    conn.execute("UPDATE audit_events SET stage='forged' WHERE run_id=?", (r.run_id,))
    conn.commit()
    with pytest.raises(ServiceError) as ei:
        service.verify_audit_chain()
    assert ei.value.code is Failure.BAD_SIGNATURE
    problems = {f["problem"] for f in ei.value.details}
    assert "entry-hash-mismatch" in problems


def test_tampering_with_run_payload_fails_signature_verification(service):
    r = service.run_diff(fx.EMPTY, fx.PHOTO_V2_EXPAND)
    assert service.verify_run_signature(r.run_id)["valid"] is True
    conn = service.store.conn
    row = conn.execute("SELECT result_json FROM runs WHERE run_id=?", (r.run_id,)).fetchone()
    tampered = json.loads(row["result_json"])
    tampered["expands"] = False
    conn.execute("UPDATE runs SET result_json=? WHERE run_id=?",
                 (json.dumps(tampered), r.run_id))
    conn.commit()
    assert service.verify_run_signature(r.run_id)["valid"] is False


def test_workspace_rejects_silent_signing_key_replacement(tmp_path):
    cfg = Config(data_dir=str(tmp_path), db_path=str(tmp_path / "db.sqlite3"),
                 key_path=str(tmp_path / "k.pem"))
    Service(cfg).close()
    # A different key file against the same database must be refused.
    cfg2 = Config(data_dir=str(tmp_path), db_path=str(tmp_path / "db.sqlite3"),
                  key_path=str(tmp_path / "other.pem"))
    with pytest.raises(Exception):
        Service(cfg2)


def test_separate_databases_are_independent(tmp_path):
    cfg_a = Config(data_dir=str(tmp_path / "a"), db_path=str(tmp_path / "a/db.sqlite3"),
                   key_path=str(tmp_path / "a/k.pem"))
    cfg_b = Config(data_dir=str(tmp_path / "b"), db_path=str(tmp_path / "b/db.sqlite3"),
                   key_path=str(tmp_path / "b/k.pem"))
    with Service(cfg_a) as a, Service(cfg_b) as b:
        a.run_diff(fx.EMPTY, fx.PHOTO_V2_EXPAND)
        assert a.list_runs() and b.list_runs() == []
