//! Bounded loops: exhaustive agreement and loop-unroll unknown handling.

use std::collections::BTreeMap;

use se_integration::common::{self, parse, run_with_z3, loop_counter, loop_over_budget};
use se_lang::interp::{self, FlowOutcome, RunOpts};
use se_verify::oracle::exhaustive_oracle;

#[test]
fn bounded_counting_loop_matches_oracle() {
    let program = parse(&loop_counter(10));
    let report = run_with_z3(&program, common::cfg(512, 64));
    assert_eq!(report.verdict, "holds");
    assert!(report.cuts.is_empty(), "{:#?}", report.cuts);
    // 0..=10 exit paths (n+1 terminals): one per n.
    assert_eq!(report.budget.explored_terminals, 11);
    assert_eq!(report.budget.max_unroll_observed, 10);

    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "holds");
    assert_eq!(oracle.completed, 11);

    // Concretely every n terminates with i == n.
    for n in 0..=10u64 {
        let mut inputs = BTreeMap::new();
        inputs.insert("n".to_string(), n);
        let r = interp::run(&program, &inputs, RunOpts::default());
        assert_eq!(r.outcome, FlowOutcome::Completed);
        assert_eq!(r.final_store["i"], n);
    }
}

#[test]
fn exceeding_unroll_budget_returns_unknown_not_holds() {
    // n up to 30 while max_loop_unroll is only 8: paths n>8 cannot be fully explored.
    let program = parse(&loop_over_budget(30));
    let report = run_with_z3(&program, common::cfg(4096, 8));
    assert_eq!(
        report.verdict,
        "unknown",
        "incomplete exploration must be reported unknown, got {:#?}",
        report.verdict
    );
    assert!(
        report.cuts.iter().any(|c| matches!(
            c.kind,
            se_engine::CutKind::LoopUnroll
        )),
        "expected a loop_unroll cut: {:#?}",
        report.cuts
    );
    assert!(report.budget.max_unroll_observed >= 8);

    // Budget values are recorded in the report.
    assert_eq!(report.budget.max_loop_unroll, 8);
}

#[test]
fn path_budget_exhaustion_is_unknown() {
    let program = parse(&loop_counter(60));
    let report = run_with_z3(&program, common::cfg(8, 128));
    assert_eq!(report.verdict, "unknown");
    assert!(
        report.cuts.iter().any(|c| matches!(
            c.kind,
            se_engine::CutKind::PathBudget
        )),
        "expected path_budget cut: {:#?}",
        report.cuts
    );
    assert!(report.budget.explored_terminals <= 8);
}
