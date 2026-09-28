"""Storage + offline replay tests.

Every category is persisted; replay into a fresh kernel reproduces exact
statuses and the evidence set; the independent checker validates each stored
bundle; malformed/tampered journal divergence is detected (never OK).
"""
from __future__ import annotations

import copy
import json

from localffg.logging_utils import JsonRunLogger
from localffg.models import SignedVote
from localffg.replay import replay_store


def _run_all_scenarios(svc, manifest, *, scenarios=None):
    scenarios = scenarios or sorted(manifest["scenarios"])
    rows = []
    for name in scenarios:
        for step in manifest["scenarios"][name]:
            signed = SignedVote.from_json_dict(step["signed"])
            rows.append((name, step["label"], svc.submit(signed)))
    return rows


def test_all_categories_are_journaled(make_service, manifest):
    # Invalid submissions do not enter state, so they share a journal safely.
    invalid_scenarios = [
        "S5_bad_signature", "S5b_wrong_signer_pubkey", "S6_unknown_validator",
        "S7_wrong_chain", "S8_bad_rounds",
    ]
    svc = make_service(manifest["registry"])
    _run_all_scenarios(svc, manifest, scenarios=invalid_scenarios)
    counts = svc.store.status_counts()
    assert set(counts) == {
        "invalid_signature", "unknown_validator", "invalid_chain", "invalid_rounds"
    }
    assert counts["invalid_signature"] == 2
    assert counts["unknown_validator"] == 1
    assert counts["invalid_chain"] == 1
    assert counts["invalid_rounds"] == 1

    # valid-but-membership-invalid votes also never enter state -> separate svc
    svc2 = make_service(manifest["registry"])
    _run_all_scenarios(svc2, manifest, scenarios=["S4_membership_change"])
    counts2 = svc2.store.status_counts()
    assert counts2 == {"invalid_membership": 2, "accepted": 2}

    # retransmission and offense counts on dedicated services (fresh state per
    # scenario because S3 and S3b replay the same validator's two votes)
    svc3 = make_service(manifest["registry"])
    _run_all_scenarios(svc3, manifest, scenarios=["S1_duplicate_retransmit"])
    c3 = svc3.store.status_counts()
    assert c3 == {"accepted": 1, "duplicate_retransmit": 2}

    svc_dv = make_service(manifest["registry"])
    _run_all_scenarios(svc_dv, manifest, scenarios=["S2_double_vote_same_target"])
    assert svc_dv.store.status_counts() == {"accepted": 1, "double_vote": 1}

    for scen in ("S3_surround_nested", "S3b_surround_reverse_order"):
        svc_s = make_service(manifest["registry"])
        _run_all_scenarios(svc_s, manifest, scenarios=[scen])
        c_s = svc_s.store.status_counts()
        assert c_s == {"accepted": 1, "surround_vote": 1}, (scen, c_s)
        assert svc_s.store.evidence_count() == 1

    svc4 = svc_dv
    assert svc4.store.evidence_count() == 1


def test_replay_reproduces_exact_statuses_and_evidence(make_service, manifest):
    # Group scenarios by shared validator state so replays are deterministic:
    # invalid scenarios never enter state; valid scenarios get isolated dbs.
    groups = [
        ["S1_duplicate_retransmit"],
        ["S2_double_vote_same_target"],
        ["S3_surround_nested"],
        ["S3b_surround_reverse_order"],
        ["S3c_boundary_equality_no_offense"],
        ["S3d_crossing_no_surround"],
        ["S4_membership_change"],
        ["S5_bad_signature", "S5b_wrong_signer_pubkey", "S6_unknown_validator",
         "S7_wrong_chain", "S8_bad_rounds"],
        ["S9_cross_validator_no_conflict"],
        ["S10_weight_snapshot_offense"],
        ["S11_mixed_conflict_then_rexmit"],
    ]
    total_events = 0
    total_evidence = 0
    for group in groups:
        svc = make_service(manifest["registry"])
        _run_all_scenarios(svc, manifest, scenarios=group)
        report = replay_store(svc.store)
        assert report.verdict == "OK", (group, report.errors)
        assert report.status_mismatches == [], group
        assert report.evidence_set_match is True, group
        assert all(c["verdict"] == "valid" for c in report.checker_results), group
        total_events += report.events_replayed
        total_evidence += len(report.checker_results)

    # concrete totals: S2(1) + S3(1) + S3b(1) + S10(1) + S11(1) = 5 evidence
    assert total_evidence == 5
    assert total_events > 20


def test_replay_from_a_closed_db_is_stable(make_service, manifest, tmp_path):
    svc = make_service(manifest["registry"])
    _run_all_scenarios(svc, manifest, scenarios=["S2_double_vote_same_target", "S3_surround_nested"])
    db_path = svc.config.db_path
    svc.close()

    from localffg.storage import VoteStore

    with VoteStore(db_path) as store2:
        report = replay_store(store2)
    assert report.verdict == "OK", report.errors
    assert report.evidence_set_match is True


def test_replay_detects_tampered_status(make_service, manifest):
    svc = make_service(manifest["registry"])
    _run_all_scenarios(svc, manifest, scenarios=["S2_double_vote_same_target"])
    # tamper the journal: pretend the double vote was "accepted"
    row = svc.store._conn.execute(
        "SELECT seq FROM ingest_events WHERE status='double_vote'"
    ).fetchone()
    assert row is not None
    svc.store._conn.execute(
        "UPDATE ingest_events SET status='accepted' WHERE seq=?", (row["seq"],)
    )
    svc.store._conn.commit()

    report = replay_store(svc.store)
    assert report.verdict == "FAIL"
    assert any(m["seq"] == row["seq"] for m in report.status_mismatches)
    mismatch = next(m for m in report.status_mismatches if m["seq"] == row["seq"])
    assert mismatch["stored_status"] == "accepted"
    assert mismatch["replayed_status"] == "double_vote"


def test_replay_detects_injected_fake_evidence(make_service, manifest):
    svc = make_service(manifest["registry"])
    _run_all_scenarios(svc, manifest, scenarios=["S2_double_vote_same_target"])
    real = svc.list_evidence()[0]
    fake = copy.deepcopy(real)
    fake["evidence_id"] = "ev_fabricated00000000000000000000000"
    fake["weight"] = 4242
    svc.store._conn.execute(
        "INSERT INTO evidence(evidence_id, kind, chain_id, validator_id, weight_epoch, "
        "weight, created_seq, created_utc, bundle_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            fake["evidence_id"], fake["kind"], fake["chain_id"], fake["validator_id"],
            fake["weight_epoch"], fake["weight"], 0, "1970-01-01T00:00:00+00:00",
            json.dumps(fake, sort_keys=True),
        ),
    )
    svc.store._conn.commit()
    report = replay_store(svc.store)
    assert report.verdict == "FAIL"
    assert not report.evidence_set_match or any(
        "ev_fabricated" in e for e in report.errors
    )


def test_replay_logs_carry_run_identity_and_progress(make_service, manifest):
    svc = make_service(manifest["registry"])
    _run_all_scenarios(svc, manifest, scenarios=["S1_duplicate_retransmit"])
    logger = JsonRunLogger(run_id="corr-run-42", echo=False)
    report = replay_store(svc.store, logger=logger)
    assert report.verdict == "OK"
    step_records = [r for r in logger.records if r["event"] == "replay_step"]
    assert len(step_records) == 3
    for r in step_records:
        assert r["run_id"] == "corr-run-42"
        assert r["app_version"] and r["protocol_version"]
        assert r["progress"]["of"] == 3
        assert r["match"] is True and r["basis"]
