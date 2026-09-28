"""State machine, content-fingerprint baseline, known-fixed classification."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from secretscan.errors import NotFoundError, StateConflictError
from secretscan.models import (
    STATE_ACTIVE,
    STATE_BASELINE_EXEMPT,
    STATE_KNOWN_FIXED,
    STATE_NEW,
    STATE_REINTRODUCED,
)
from secretscan.service import ScanService
from secretscan.storage import Store

CONFIG = "config/rules.yaml"
AWS = "AKIAFAKE000000000001"
GHP = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture()
def service(tmp_path):
    store = Store(tmp_path / "state")
    svc = ScanService(store, CONFIG)
    yield svc
    store.close()


def make_snapshot(root: Path, *, aws: bool = True, ghp: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if aws:
        (root / "a.txt").write_text(f"key={AWS}\n")
    if ghp:
        (root / "b.txt").write_text(f"{GHP}\n")
    return root


def _fps(result, rule_id):
    return [c["fingerprint"] for c in result["candidates"] if c["rule_id"] == rule_id]


def test_new_then_active_state_transitions(tmp_path, service):
    snap = make_snapshot(tmp_path / "snap1")
    r1 = service.run_scan(snap, actor="alice", request_id="req-1")
    states = {c["rule_id"]: c["state"] for c in r1["candidates"]}
    assert states["aws_access_key_id"] == STATE_NEW
    assert states["github_personal_token"] == STATE_NEW

    r2 = service.run_scan(snap, actor="alice", request_id="req-2")
    states2 = {c["rule_id"]: c["state"] for c in r2["candidates"]}
    assert states2["aws_access_key_id"] == STATE_ACTIVE
    assert states2["github_personal_token"] == STATE_ACTIVE


def test_deleted_candidate_becomes_known_fixed(tmp_path, service):
    snap = make_snapshot(tmp_path / "snap2")
    service.run_scan(snap, actor="alice", request_id="req-1")
    service.run_scan(snap, actor="alice", request_id="req-1b")  # new -> active

    # Delete the AWS line; GHP remains.
    (snap / "a.txt").write_text("key=removed\n")
    r2 = service.run_scan(snap, actor="alice", request_id="req-2")

    assert len(r2["known_fixed"]) == 1
    kf = r2["known_fixed"][0]
    assert kf["rule_id"] == "aws_access_key_id"
    assert kf["previous_state"] == STATE_ACTIVE
    assert kf["fixed_in_scan_id"] == r2["scan_id"]
    assert kf["was_baseline_exempt"] is False
    # Masked only — no raw value.
    assert AWS not in json.dumps(kf)
    assert kf["masked"] == "AKIA…01"

    candidates = {c["rule_id"]: c for c in service.list_candidates(r2["project_id"])}
    assert candidates["aws_access_key_id"]["state"] == STATE_KNOWN_FIXED


def test_reintroduction_after_fix(tmp_path, service):
    snap = make_snapshot(tmp_path / "snap3", ghp=False)
    service.run_scan(snap, actor="alice", request_id="req-1")
    (snap / "a.txt").write_text("nothing here\n")
    service.run_scan(snap, actor="alice", request_id="req-2")  # -> known_fixed
    (snap / "a.txt").write_text(f"key={AWS}\n")
    r3 = service.run_scan(snap, actor="alice", request_id="req-3")
    states = {c["rule_id"]: c["state"] for c in r3["candidates"]}
    assert states["aws_access_key_id"] == STATE_REINTRODUCED


def test_baseline_binds_content_not_filename(tmp_path, service):
    snap = make_snapshot(tmp_path / "snap4", ghp=False)
    r1 = service.run_scan(snap, actor="alice", request_id="req-1")
    pid = r1["project_id"]
    fp = _fps(r1, "aws_access_key_id")[0]

    out = service.accept_baseline(
        pid, rule_id="aws_access_key_id", fingerprint=fp,
        actor="alice", request_id="req-b", note="fake fixture key",
    )
    assert out["state"] == STATE_BASELINE_EXEMPT

    # Move to a new file name, keep identical content -> still exempt.
    (snap / "a.txt").rename(snap / "moved.txt")
    r2 = service.run_scan(snap, actor="alice", request_id="req-2")
    cand = next(c for c in r2["candidates"] if c["rule_id"] == "aws_access_key_id")
    assert cand["state"] == STATE_BASELINE_EXEMPT
    assert cand["locations"][0]["path"] == "moved.txt"

    # Different content at the same path -> brand new candidate, not exempt.
    (snap / "moved.txt").write_text("key=AKIAFAKE000000000099\n")
    r3 = service.run_scan(snap, actor="alice", request_id="req-3")
    aws = [c for c in r3["candidates"] if c["rule_id"] == "aws_access_key_id"]
    assert len(aws) == 1
    assert aws[0]["state"] == STATE_NEW
    assert aws[0]["masked"] == "AKIA…99"


def test_baseline_accept_unknown_fingerprint_rejected(tmp_path, service):
    snap = make_snapshot(tmp_path / "snap5", ghp=False)
    r1 = service.run_scan(snap, actor="alice", request_id="req-1")
    with pytest.raises(NotFoundError) as exc:
        service.accept_baseline(
            r1["project_id"], rule_id="aws_access_key_id", fingerprint="deadbeef",
            actor="alice",
        )
    assert "content fingerprint" in exc.value.message


def test_baseline_then_disappearance_was_exempt_flag(tmp_path, service):
    snap = make_snapshot(tmp_path / "snap6", ghp=False)
    r1 = service.run_scan(snap, actor="alice", request_id="req-1")
    service.run_scan(snap, actor="alice", request_id="req-1b")  # settle to active
    pid = r1["project_id"]
    fp = _fps(r1, "aws_access_key_id")[0]
    service.accept_baseline(pid, rule_id="aws_access_key_id", fingerprint=fp, actor="alice")
    (snap / "a.txt").unlink()
    r2 = service.run_scan(snap, actor="alice", request_id="req-2")
    kf = r2["known_fixed"][0]
    assert kf["was_baseline_exempt"] is True


def test_accept_against_known_fixed_is_conflict(tmp_path, service):
    snap = make_snapshot(tmp_path / "snap7", ghp=False)
    r1 = service.run_scan(snap, actor="alice", request_id="req-1")
    fp = _fps(r1, "aws_access_key_id")[0]
    (snap / "a.txt").unlink()
    service.run_scan(snap, actor="alice", request_id="req-2")
    service.run_scan(snap, actor="alice", request_id="req-2b")
    with pytest.raises(StateConflictError):
        service.accept_baseline(
            r1["project_id"], rule_id="aws_access_key_id", fingerprint=fp, actor="alice"
        )


def test_project_state_is_isolated_in_separate_databases(tmp_path, service):
    s1 = make_snapshot(tmp_path / "p1", ghp=False)
    s2 = make_snapshot(tmp_path / "p2", aws=False, ghp=True)
    r1 = service.run_scan(s1, actor="a", request_id="r1")
    r2 = service.run_scan(s2, actor="a", request_id="r2")
    assert r1["project_id"] != r2["project_id"]

    # Each project sees only its own candidates.
    p1c = service.list_candidates(r1["project_id"])
    p2c = service.list_candidates(r2["project_id"])
    assert {c["rule_id"] for c in p1c} == {"aws_access_key_id"}
    assert {c["rule_id"] for c in p2c} == {"github_personal_token"}

    # Separate SQLite files on disk.
    db1 = service.store.project_db_path(r1["project_id"])
    db2 = service.store.project_db_path(r2["project_id"])
    assert db1 != db2 and db1.is_file() and db2.is_file()

    # Same content in another project has an unrelated fingerprint.
    s3 = tmp_path / "p3"
    make_snapshot(s3, ghp=False)
    r3 = service.run_scan(s3, actor="a", request_id="r3")
    fp1 = _fps(r1, "aws_access_key_id")[0]
    fp3 = _fps(r3, "aws_access_key_id")[0]
    assert fp1 != fp3


def test_audit_trail_correlates_request_identity(tmp_path, service):
    snap = make_snapshot(tmp_path / "p9", ghp=False)
    service.run_scan(snap, actor="alice", request_id="req-trace-42")
    pid = service.store.list_projects()[0]["project_id"]
    events = service.audit_trail(pid)
    scan_events = [e for e in events if e["action"] == "scan.run"]
    assert len(scan_events) == 1
    e = scan_events[0]
    assert e["request_id"] == "req-trace-42"
    assert e["actor"] == "alice"
    assert e["detail"]["rules_version"] == "2026.09.01"
    assert e["detail"]["candidates_total"] >= 1


def test_unknown_scan_and_project_failures(tmp_path, service):
    with pytest.raises(NotFoundError):
        service.latest_scan("proj_doesnotexist")
