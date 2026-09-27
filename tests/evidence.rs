//! Evidence verification tests.
//!
//! A genuine bundle produced from a real run verifies; bundles with a
//! tampered verdict, tampered obligation or a stale cross-version snapshot
//! must be reported with the precise failing check rather than accepted.

mod common;

use bounded_monitor::evidence::EvidenceBundle;
use bounded_monitor::kernel::{Limits, Monitor, ObligationStatus, Verdict};
use bounded_monitor::language::Ruleset;
use bounded_monitor::oracle;
use common::{log_event, run_online};

fn shop() -> Ruleset {
    common::load_ruleset("shop-v1.json")
}

fn bundle_for(run_id: &str, trace_name: &str, cut: Option<usize>) -> EvidenceBundle {
    let ruleset = shop();
    let steps = common::load_trace(trace_name);
    let cut = cut.unwrap_or(steps.len());
    let mut m = Monitor::new(ruleset.clone(), Limits::default()).unwrap();
    for s in &steps[..cut] {
        m.apply_step(s).unwrap();
    }
    let snapshot = m.snapshot();
    EvidenceBundle {
        run_id: run_id.to_string(),
        ruleset: ruleset.clone(),
        trace: steps.clone(),
        snapshot_after_index: Some(cut as u64),
        snapshot: Some(snapshot),
        online_obligations: m.obligations(),
        claimed_verdict: {
            // Claim the verdict AFTER driving the full trace, as a real
            // producer does.
            let mut full = Monitor::new(ruleset, Limits::default()).unwrap();
            for s in &steps {
                full.apply_step(s).unwrap();
            }
            full.verdict()
        },
    }
}

#[test]
fn genuine_bundles_verify_with_and_without_cut() {
    for (name, trace) in [
        ("ev-a", "a_boundary_satisfied.json"),
        ("ev-b", "b_overlap_triggers.json"),
        ("ev-d", "d_early_close.json"),
        ("ev-f", "f_sustain_overlap_ok.json"),
    ] {
        // Cut in the middle: restore + suffix must reproduce the verdict.
        let bundle = bundle_for(name, trace, Some(2));
        let report = bundle.verify().unwrap();
        assert!(report.valid, "{name}: genuine bundle failed: {:?}", report.failures);
        assert!(report.restored_verdict.is_some());
        assert_eq!(report.restored_verdict, Some(report.oracle_verdict));
        log_event(name, "evidence", "verified_with_cut", format!("{trace}: {:?}", report.failures));

        // No snapshot: obligations describe the end state.
        let mut end_bundle = bundle.clone();
        end_bundle.snapshot = None;
        end_bundle.snapshot_after_index = None;
        end_bundle.online_obligations = {
            let ruleset = shop();
            let steps = common::load_trace(trace);
            run_online(name, "evidence_no_cut", &ruleset, &steps, Limits::default()).obligations()
        };
        let report = end_bundle.verify().unwrap();
        assert!(report.valid, "{name} no-cut: {:?}", report.failures);
        assert_eq!(report.restored_verdict, None);
    }
}

#[test]
fn tampered_claimed_verdict_is_detected() {
    let mut bundle = bundle_for("ev-tamper-verdict", "a_boundary_satisfied.json", Some(2));
    assert_eq!(bundle.claimed_verdict, Verdict::Satisfied);
    bundle.claimed_verdict = Verdict::Violated;
    let report = bundle.verify().unwrap();
    assert!(!report.valid);
    assert!(
        report.failures.iter().any(|f| f.check == "claimed_verdict_vs_oracle"),
        "expected claimed_verdict_vs_oracle in {:?}",
        report.failures
    );
    log_event("ev-tamper-verdict", "evidence", "detected", "claimed verdict flipped to violated -> claimed_verdict_vs_oracle");
}

#[test]
fn tampered_obligation_status_is_detected() {
    let mut bundle = bundle_for("ev-tamper-obl", "a_boundary_satisfied.json", Some(2));
    // At the cut (2 steps applied) ack#o0 was resolved at s1; flip it.
    let obl = bundle
        .online_obligations
        .iter_mut()
        .find(|o| o.id == "ack_order#o0")
        .expect("ack obligation at cut");
    assert_eq!(obl.status, ObligationStatus::Satisfied);
    obl.status = ObligationStatus::Pending;
    obl.resolution_step = None;
    let report = bundle.verify().unwrap();
    assert!(!report.valid);
    assert!(
        report.failures.iter().any(|f| f.check == "obligations_at_cut"),
        "expected obligations_at_cut mismatch, got {:?}",
        report.failures
    );
    log_event("ev-tamper-obl", "evidence", "detected", "ack#o0 flipped satisfied->pending -> obligations_at_cut");
}

#[test]
fn snapshot_from_another_ruleset_version_is_rejected() {
    let bundle = bundle_for("ev-stale-snapshot", "a_boundary_satisfied.json", Some(2));
    let mut newer = shop();
    newer.version = "9.0.0".to_string();

    // Restoring the recorded snapshot under the new ruleset is a state
    // conflict (state conflict, not an evidence mismatch — old rule state
    // must never silently run against new rules).
    let err = Monitor::restore(bundle.snapshot.clone().unwrap(), &newer).unwrap_err();
    use bounded_monitor::error::ErrorKind;
    assert_eq!(err.kind, ErrorKind::StateConflict);
    assert_eq!(err.reason, "ruleset_version_mismatch");
    log_event("ev-stale-snapshot", "evidence", "restore_rejected", "snapshot v1 vs ruleset v9 -> ruleset_version_mismatch");

    // And feeding the mismatched pair inside a bundle surfaces the version
    // mismatch check during verification too.
    let mut mixed = bundle.clone();
    mixed.ruleset = newer;
    let report = mixed.verify().unwrap();
    assert!(!report.valid);
    assert!(report
        .failures
        .iter()
        .any(|f| f.check == "snapshot_ruleset_version" || f.check == "snapshot_ruleset_hash"));
}

#[test]
fn oracle_and_kernel_agree_on_every_fixture_without_evidence() {
    // Belt-and-braces: run oracle directly for every JSON fixture and
    // compare against the kernel obligation-by-obligation.
    let ruleset = shop();
    for trace in [
        "a_boundary_satisfied.json",
        "b_overlap_triggers.json",
        "c_missing_response.json",
        "d_early_close.json",
        "e_sustain_break.json",
        "f_sustain_overlap_ok.json",
        "g_empty_closed.json",
    ] {
        let steps = common::load_trace(trace);
        let report = oracle::evaluate(&ruleset, &steps).unwrap();
        let m = run_online("direct-xcheck", "evidence_direct", &ruleset, &steps, Limits::default());
        assert_eq!(m.verdict(), report.verdict, "{trace} verdict");
        let online: std::collections::BTreeMap<_, _> =
            m.obligations().into_iter().map(|o| (o.id.clone(), o)).collect();
        assert_eq!(online.len(), report.obligations.len(), "{trace} count");
        for o in &report.obligations {
            let v = &online[&o.id];
            assert_eq!(format!("{:?}", v.status), format!("{:?}", o.status), "{trace} {} status", o.id);
            assert_eq!(v.trigger_step, o.trigger_step, "{trace} {} trigger", o.id);
            assert_eq!(v.deadline_step, o.deadline_step, "{trace} {} deadline", o.id);
            assert_eq!(v.resolution_step, o.resolution_step, "{trace} {} resolution", o.id);
            assert_eq!(v.violation_step, o.violation_step, "{trace} {} violation", o.id);
        }
    }
}
