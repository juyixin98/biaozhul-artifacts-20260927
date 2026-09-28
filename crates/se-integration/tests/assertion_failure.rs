//! Scenario 1: assertion failure.
//!
//! Engine verdict, counterexample value range, independent concrete replay at the
//! exact failing statement, and small-domain exhaustive oracle all agree.

use std::collections::BTreeMap;

use se_integration::common::{self, parse, run_with_z3, z3_available, ASSERT_FAIL};
use se_lang::interp::{self, FailureKind, FlowOutcome, RunOpts};
use se_verify::oracle::exhaustive_oracle;
use se_verify::verify_report;

#[test]
fn assertion_failure_is_found_replayed_and_matches_oracle() {
    let program = parse(ASSERT_FAIL);
    let report = run_with_z3(&program, common::cfg(64, 16));

    // Concrete expected result: engine must say violation.
    assert_eq!(report.verdict, "violation");
    assert_eq!(report.evidence.len(), 1, "exactly one failing site expected");
    let ev = &report.evidence[0];
    assert_eq!(ev.failure, FailureKind::Assertion);
    assert_eq!(ev.stmt_id, 0);

    // Witness must be a genuinely bad input: x in 11..=255.
    let x = ev.inputs["x"];
    assert!((11..=255).contains(&x), "witness x={x} should violate x<=10");

    // Independent replay reproduces the same failure category and statement.
    let mut inputs = BTreeMap::new();
    inputs.insert("x".to_string(), x);
    let replay = interp::run(&program, &inputs, RunOpts::default());
    match replay.outcome {
        FlowOutcome::Failed(f) => {
            assert_eq!(f.kind, FailureKind::Assertion);
            assert_eq!(f.stmt_id, 0);
        }
        other => panic!("expected assertion failure, got {other:?}"),
    }

    // Post-replay verdict must remain violation with one confirmed, zero rejected.
    let verified = verify_report(&program, &report, RunOpts::default());
    assert_eq!(verified.final_verdict, "violation");
    assert_eq!(verified.confirmed_count, 1);
    assert_eq!(verified.rejected_count, 0);

    // Oracle ground truth over the full 256 domain.
    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "violation");
    assert_eq!(oracle.total_assignments, 256);
    assert_eq!(oracle.failures.len(), 245);
    let bad: std::collections::BTreeSet<u64> =
        oracle.failures.iter().map(|f| f.inputs["x"]).collect();
    let expected: std::collections::BTreeSet<u64> = (11..=255).collect();
    assert_eq!(bad, expected);
    assert!(bad.contains(&x), "engine witness must be oracle-bad");
    assert_eq!(oracle.failure_sites, vec![("assertion".to_string(), 0)]);
}

#[test]
fn all_oracle_bad_inputs_replay_as_assertion_at_stmt_0() {
    let program = parse(ASSERT_FAIL);
    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    for f in &oracle.failures {
        let replay = interp::run(&program, &f.inputs, RunOpts::default());
        assert_eq!(
            replay.outcome,
            FlowOutcome::Failed(interp::Failure {
                kind: FailureKind::Assertion,
                stmt_id: 0,
                op: None
            }),
            "input {:?}",
            f.inputs
        );
    }
    // The safe prefix 0..=10 completes.
    for x in 0..=10u64 {
        let mut inputs = BTreeMap::new();
        inputs.insert("x".to_string(), x);
        let r = interp::run(&program, &inputs, RunOpts::default());
        assert_eq!(r.outcome, FlowOutcome::Completed, "x={x}");
    }
}

#[test]
fn log_events_carry_identity_and_decisions() {
    let _ = z3_available();
    let program = parse(ASSERT_FAIL);
    let report = run_with_z3(&program, common::cfg(64, 16));
    // Engine/version present, events ordered and decision-bearing.
    assert!(report.engine_version.starts_with("se-engine"));
    let kinds: Vec<&str> = report.steps.iter().map(|s| s.kind.as_str()).collect();
    assert_eq!(*kinds.first().unwrap(), "start");
    assert_eq!(*kinds.last().unwrap(), "finish");
    assert!(kinds.contains(&"violation"));
    assert!(kinds.contains(&"check"));
    // Every check records its reasoning basis.
    assert!(report
        .steps
        .iter()
        .filter(|s| s.kind == "check")
        .all(|s| s.detail.contains("sat") || s.detail.contains("unsat") || s.detail.contains("unknown")));
}
