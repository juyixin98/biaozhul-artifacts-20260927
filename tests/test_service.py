"""Service-layer tests: replayability, signing and state isolation."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import NotFoundError, StateConflictError
from app.service import AuditService

from conftest import BROKEN_POLICY, FIXED_POLICY, FIXTURES_DIR


def _file_service(tmp_path: Path) -> AuditService:
    return AuditService(
        tmp_path / "state" / "audit.db",
        fixtures_dir=FIXTURES_DIR,
        key_dir=tmp_path / "keys",
    )


def test_run_secret_derivation_is_stable_across_restarts(tmp_path):
    """HKDF-derived per-run keys must be reproducible after a restart
    (master secret persisted), while different run ids diverge."""
    svc = _file_service(tmp_path)
    secret_a_1 = svc._run_secret("run-A")
    secret_b = svc._run_secret("run-B")
    assert len(secret_a_1) == 32 and secret_a_1 != secret_b
    svc.close()

    svc2 = _file_service(tmp_path)
    assert svc2._run_secret("run-A") == secret_a_1  # replayable
    assert svc2._run_secret("run-B") == secret_b
    svc2.close()


def test_replayed_audit_produces_identical_findings(tmp_path):
    svc = _file_service(tmp_path)
    r1 = svc.run_audit(BROKEN_POLICY, fixture="language", run_id="run-one")
    e1 = svc.get_events("run-one")
    svc.close()

    svc2 = _file_service(tmp_path)
    r2 = svc2.run_audit(BROKEN_POLICY, fixture="language", run_id="run-two")
    e2 = svc2.get_events("run-two")

    assert r1["summary"] == r2["summary"]
    # Same evidence shape -> same sequence of intermediate states, apart
    # from the run-id-scoped key prefixes.
    assert [e["event"] for e in e1] == [e["event"] for e in e2]
    svc2.close()


def test_duplicate_run_id_conflicts(service):
    service.run_audit(BROKEN_POLICY, fixture="language", run_id="run-x")
    with pytest.raises(StateConflictError) as exc:
        service.run_audit(BROKEN_POLICY, fixture="language", run_id="run-x")
    assert exc.value.code == "run.duplicate"


def test_missing_report_not_found(service):
    with pytest.raises(NotFoundError) as exc:
        service.get_report("run-nope")
    assert exc.value.code == "run.not_found"


def test_request_limit_resource_error(service):
    from app.errors import ResourceExhaustedError
    with pytest.raises(ResourceExhaustedError) as exc:
        service.run_audit(
            {**BROKEN_POLICY, "limits": {"max_requests": 1}},
            fixture="language",
        )
    assert exc.value.code == "limit.requests"


def test_fixed_policy_zero_collisions_all_negotiation_fixtures(service):
    for fixture in ("language", "encoding", "identity", "missing_vary",
                    "vary_wildcard"):
        report = service.run_audit(FIXED_POLICY, fixture=fixture)
        assert report["summary"]["collisions"] == 0, fixture


def test_signed_report_verifies(service):
    report = service.run_audit(BROKEN_POLICY, fixture="language")
    result = service.verify_report(report["run_id"])
    assert result["valid"] is True
    assert result["key_id"] == report["key_id"]
    assert len(report["signature"]) > 0


def test_tampered_report_fails_verification(service):
    import json

    report = service.run_audit(BROKEN_POLICY, fixture="language")
    run_id = report["run_id"]
    tampered = dict(report)
    tampered["summary"] = dict(report["summary"], collisions=999)
    service.store._conn.execute(
        "UPDATE runs SET report_json = ? WHERE run_id = ?",
        (json.dumps(tampered, sort_keys=True), run_id),
    )
    service.store._conn.commit()
    assert service.verify_report(run_id)["valid"] is False


def test_raw_credentials_are_not_persisted(tmp_path):
    svc = _file_service(tmp_path)
    svc.run_audit(BROKEN_POLICY, fixture="identity", run_id="run-secret-check")
    db = (tmp_path / "state" / "audit.db").read_bytes()
    assert b"synthetic-token-alice" not in db
    assert b"synthetic-token-bob" not in db
    svc.close()


def test_runs_are_isolated_by_run_id(service):
    r1 = service.run_audit(BROKEN_POLICY, fixture="language", run_id="run-iso-1")
    r2 = service.run_audit(FIXED_POLICY, fixture="language", run_id="run-iso-2")
    assert r1["summary"]["collisions"] == 1
    assert r2["summary"]["collisions"] == 0
    assert service.get_events("run-iso-1") != service.get_events("run-iso-2")
