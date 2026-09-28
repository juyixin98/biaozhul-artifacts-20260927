//! Scenario 4: infeasible branches and division-by-zero guards.
//!
//! * An unreachable branch containing `assert 0` must not yield a violation.
//! * A reachable divisor == 0 must be classified as `div_by_zero` at the assignment.
//! * Division by zero on a provably infeasible branch must be pruned (holds).

use std::collections::BTreeMap;

use se_integration::common::{
    self, parse, run_with_z3, DIV_BY_ZERO, DIV_ZERO_INFEASIBLE, INFEASIBLE_BRANCH,
};
use se_lang::interp::{self, FailureKind, FlowOutcome, RunOpts};
use se_verify::oracle::exhaustive_oracle;
use se_verify::verify_report;

#[test]
fn dead_branch_does_not_fire() {
    let program = parse(INFEASIBLE_BRANCH);
    let report = run_with_z3(&program, common::cfg(64, 16));
    assert_eq!(report.verdict, "holds", "dead assert 0 must be unreachable");
    assert!(report.evidence.is_empty());
    assert!(report.cuts.is_empty(), "unexpected cuts: {:#?}", report.cuts);

    // Oracle: x in 0..=2 all complete; x >= 3 killed by the assume.
    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "holds");
    assert_eq!(oracle.completed, 3);
    assert_eq!(oracle.infeasible_assume, 253);
    assert!(oracle.failures.is_empty());

    // Direct concrete checks around the boundary.
    for x in 0u64..=4 {
        let mut inputs = BTreeMap::new();
        inputs.insert("x".to_string(), x);
        let r = interp::run(&program, &inputs, RunOpts::default());
        match x {
            0..=2 => assert_eq!(r.outcome, FlowOutcome::Completed, "x={x}"),
            _ => assert_eq!(r.outcome, FlowOutcome::InfeasibleAssume, "x={x}"),
        }
    }
}

#[test]
fn division_by_zero_is_typed_failure_at_assignment() {
    let program = parse(DIV_BY_ZERO);
    // Concretely, only x == 3 makes d == 0.
    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "violation");
    assert_eq!(oracle.failures.len(), 1);
    let f = &oracle.failures[0];
    assert_eq!(f.kind, "div_by_zero");
    assert_eq!(f.stmt_id, 1);
    assert_eq!(f.inputs["x"], 3);

    let mut inputs = BTreeMap::new();
    inputs.insert("x".to_string(), 3u64);
    let r = interp::run(&program, &inputs, RunOpts::default());
    assert_eq!(
        r.outcome,
        FlowOutcome::Failed(interp::Failure {
            kind: FailureKind::DivByZero,
            stmt_id: 1,
            op: Some("udiv")
        })
    );

    // Neighboring divisor values divide fine. Here `x` is the input; d = x - 3, so
    // d != 0 means x != 3.
    for x in [0u64, 1, 2, 4, 6] {
        let mut inputs = BTreeMap::new();
        inputs.insert("x".to_string(), x);
        let r = interp::run(&program, &inputs, RunOpts::default());
        assert_eq!(r.outcome, FlowOutcome::Completed, "x={x}");
        assert_eq!(
            r.final_store["q"],
            42 / x.wrapping_sub(3),
            "x={x}"
        );
    }

    let report = run_with_z3(&program, common::cfg(64, 16));
    assert_eq!(report.verdict, "violation");
    let ev = &report.evidence[0];
    assert_eq!(ev.failure, FailureKind::DivByZero);
    assert_eq!(ev.stmt_id, 1);
    assert_eq!(ev.inputs["x"], 3);

    let verified = verify_report(&program, &report, RunOpts::default());
    assert_eq!(verified.final_verdict, "violation");
    assert_eq!(verified.verified[0].replay_outcome, "div_by_zero");
    assert_eq!(verified.verified[0].replay_stmt, Some(1));
}

#[test]
fn division_by_zero_on_infeasible_branch_is_pruned() {
    let program = parse(DIV_ZERO_INFEASIBLE);
    let report = run_with_z3(&program, common::cfg(64, 16));
    assert_eq!(report.verdict, "holds");
    assert!(report.evidence.is_empty());

    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "holds");
    assert!(oracle.failures.is_empty());
    assert_eq!(oracle.completed, 11);
}
