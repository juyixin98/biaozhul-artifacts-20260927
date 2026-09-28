//! Scenario 3: fixed-width wrap-around and trap-mode overflow.

use std::collections::BTreeMap;

use se_integration::common::{
    self, parse, run_with_z3, TRAP_OVERFLOW, WRAP_AROUND,
};
use se_lang::interp::{self, FailureKind, FlowOutcome, RunOpts};
use se_verify::oracle::exhaustive_oracle;
use se_verify::verify_report;

#[test]
fn wrap_around_counterexample_reproduces_wrapped_value() {
    let program = parse(WRAP_AROUND);

    // Concrete semantics first (independent of the engine):
    // y = (x + 100) mod 256; assertion y > 200 fails on
    // x in [0,100] (y in [100,200]) and [156,255] (wrapped y in [0,99]).
    let mut expected_bad: BTreeMap<u64, u64> = BTreeMap::new();
    for x in 0..=255u64 {
        let y = (x + 100) & 0xff;
        if !(y > 200) {
            expected_bad.insert(x, y);
        }
    }
    assert_eq!(expected_bad.len(), 201);

    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "violation");
    let oracle_bad: std::collections::BTreeSet<u64> =
        oracle.failures.iter().map(|f| f.inputs["x"]).collect();
    assert_eq!(
        oracle_bad,
        expected_bad.keys().copied().collect(),
        "wrap failure set must match hand-computed values"
    );

    let report = run_with_z3(&program, common::cfg(64, 16));
    assert_eq!(report.verdict, "violation");
    let ev = &report.evidence[0];
    let x = ev.inputs["x"];
    assert!(expected_bad.contains_key(&x));

    // Replay must reproduce the same wrapped y and fail at the assert statement.
    let mut inputs = BTreeMap::new();
    inputs.insert("x".to_string(), x);
    let r = interp::run(&program, &inputs, RunOpts::default());
    assert_eq!(
        r.outcome,
        FlowOutcome::Failed(interp::Failure {
            kind: FailureKind::Assertion,
            stmt_id: 1,
            op: None
        })
    );
    assert_eq!(r.final_store["y"], expected_bad[&x], "wrapped value agrees");
    assert!(r.final_store["y"] <= 200);

    let verified = verify_report(&program, &report, RunOpts::default());
    assert_eq!(verified.final_verdict, "violation");
    assert_eq!(verified.confirmed_count, 1);
}

#[test]
fn trap_mode_makes_overflow_a_typed_failure() {
    let program = parse(TRAP_OVERFLOW);
    // Concrete: x + 200 overflows u8 iff x >= 56 (56+200=256); x in 56..=255 fail.
    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "violation");
    let bad: std::collections::BTreeSet<u64> =
        oracle.failures.iter().map(|f| f.inputs["x"]).collect();
    assert_eq!(bad, (56..=255u64).collect());
    assert!(oracle
        .failures
        .iter()
        .all(|f| f.kind == "overflow" && f.stmt_id == 0));
    // Below the threshold it completes.
    let mut inputs = BTreeMap::new();
    inputs.insert("x".to_string(), 55u64);
    let r = interp::run(&program, &inputs, RunOpts::default());
    assert_eq!(r.outcome, FlowOutcome::Completed);
    assert_eq!(r.final_store["y"], 255);

    // Engine finds the overflow with the correct failure category.
    let report = run_with_z3(&program, common::cfg(64, 16));
    assert_eq!(report.verdict, "violation");
    let ev = &report.evidence[0];
    assert_eq!(ev.failure, FailureKind::Overflow);
    assert_eq!(ev.stmt_id, 0);
    assert!(ev.op.is_some(), "op name attached: {:?}", ev.op);
    assert!(bad.contains(&ev.inputs["x"]));

    let verified = verify_report(&program, &report, RunOpts::default());
    assert_eq!(verified.final_verdict, "violation");
    assert_eq!(verified.verified[0].replay_outcome, "overflow");
}

#[test]
fn wrap_mode_does_not_report_overflow_as_failure() {
    // Same arithmetic with default wrap semantics must only flag the assert below.
    let src = r#"{
      "width": 8,
      "inputs": [{"name": "x", "low": 0, "high": 255}],
      "body": [
        {"stmt": "assign", "target": "y",
         "expr": {"expr": "add", "lhs": {"expr": "var", "name": "x"}, "rhs": {"expr": "int", "value": 200}}}
      ]
    }"#;
    let program = parse(src);
    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "holds");
    assert!(oracle.failures.is_empty());
    // The wrapped value is well-defined for all inputs.
    for x in [0u64, 55, 56, 200, 255] {
        let mut inputs = BTreeMap::new();
        inputs.insert("x".to_string(), x);
        let r = interp::run(&program, &inputs, RunOpts::default());
        assert_eq!(r.outcome, FlowOutcome::Completed);
        assert_eq!(r.final_store["y"], (x + 200) & 0xff);
    }
}
