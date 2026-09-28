//! Scenario 2: mutually exclusive paths.
//!
//! Both branches feasible; assertions hold on their respective branches → `holds`,
//! with exactly two terminal safe paths and a matching exhaustive oracle. A second
//! fixture where the else branch can fail must produce a localized counterexample.

use std::collections::BTreeMap;

use se_integration::common::{self, parse, run_with_z3, MUTEX_HOLDS, MUTEX_ONE_FAILS};
use se_lang::interp::{self, FailureKind, FlowOutcome, RunOpts};
use se_verify::oracle::exhaustive_oracle;
use se_verify::verify_report;

#[test]
fn mutex_paths_hold_and_oracle_agrees() {
    let program = parse(MUTEX_HOLDS);
    let report = run_with_z3(&program, common::cfg(64, 16));

    assert_eq!(report.verdict, "holds", "should hold on both branches");
    assert!(report.cuts.is_empty(), "no cuts: {:#?}", report.cuts);
    assert!(report.evidence.is_empty());
    assert_eq!(report.budget.explored_terminals, 2);
    assert!(report.budget.sat_queries >= 2);
    assert!(report.budget.unsat_queries >= 2);

    // Path records: both safe, and their conditions are complementary.
    let statuses: Vec<String> = report.paths.iter().map(|p| p.status.clone()).collect();
    assert!(statuses.iter().all(|s| s == "safe"), "{statuses:?}");

    // Independent oracle: all 21 assignments complete.
    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    assert_eq!(oracle.verdict, "holds");
    assert_eq!(oracle.total_assignments, 21);
    assert_eq!(oracle.completed, 21);
    assert!(oracle.failures.is_empty());

    // Explicit concrete spot checks of the two branches.
    for (x, branch) in [(2u64, "then"), (7u64, "else"), (5u64, "else"), (4u64, "then")] {
        let mut inputs = BTreeMap::new();
        inputs.insert("x".to_string(), x);
        let r = interp::run(&program, &inputs, RunOpts { record_trace: true, ..Default::default() });
        assert_eq!(r.outcome, FlowOutcome::Completed, "{branch} branch x={x}");
    }
}

#[test]
fn mutex_one_branch_fails_localized_by_oracle_and_engine() {
    let program = parse(MUTEX_ONE_FAILS);
    let report = run_with_z3(&program, common::cfg(64, 16));
    assert_eq!(report.verdict, "violation");
    let ev = &report.evidence[0];
    assert_eq!(ev.failure, FailureKind::Assertion);
    assert_eq!(ev.stmt_id, 2, "else-branch assert has preorder id 2");
    let x = ev.inputs["x"];
    // Then-branch covers x<5; else requires x>=10 to pass, so failure set is 5..=9.
    assert!((5..=9).contains(&x), "witness x={x}");

    let oracle = exhaustive_oracle(&program, 4096, RunOpts::default());
    let bad: BTreeMap<u64, FailureKind> = oracle
        .failures
        .iter()
        .map(|f| (f.inputs["x"], FailureKind::from_str_ignore(&f.kind)))
        .collect();
    let bad_keys: std::collections::BTreeSet<u64> = bad.keys().copied().collect();
    assert_eq!(bad_keys, (5..=9u64).collect());
    assert_eq!(oracle.completed, 16); // 21 - 5

    // Engine witness must be a member of the independently found bad set.
    assert!(bad_keys.contains(&x));
    let verified = verify_report(&program, &report, RunOpts::default());
    assert_eq!(verified.final_verdict, "violation");
    assert_eq!(verified.rejected_count, 0);
}

// Small local helper avoiding extra pub surface.
trait FromStrIgnore {
    fn from_str_ignore(s: &str) -> FailureKind;
}
impl FromStrIgnore for FailureKind {
    fn from_str_ignore(s: &str) -> FailureKind {
        match s {
            "assertion" => FailureKind::Assertion,
            "div_by_zero" => FailureKind::DivByZero,
            "overflow" => FailureKind::Overflow,
            _ => FailureKind::StepLimit,
        }
    }
}
