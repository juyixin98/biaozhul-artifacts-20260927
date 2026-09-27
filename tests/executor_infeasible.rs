//! Infeasible branches: contradictory conditions must be pruned, recorded as
//! infeasible, and must never produce a spurious failure or a "safe" claim
//! that skips exploration accounting.

mod common;

use common::*;
use symex::kernel::report::{BranchFeasibility, PathStatus, Verdict};

#[test]
fn contradictory_branch_is_pruned_and_recorded() {
    let report = analyze_src(
        r#"
        param x: u8;
        assume(x < 5u8);
        if (x > 10u8) {
            assert(false, "must be unreachable");
        }
        assert(x < 5u8);
        "#,
    );
    assert_eq!(report.verdict, Verdict::Safe);
    assert_eq!(report.path_counts.failed, 0);
    // Exactly one real path; the if-then side was proved infeasible.
    let safe = report
        .paths
        .iter()
        .find(|p| p.status == PathStatus::Safe)
        .expect("one safe path");
    let if_branch = &safe.branches[0];
    assert!(!if_branch.taken, "then-branch not taken");
    assert_eq!(if_branch.feasibility, BranchFeasibility::Infeasible);
}

#[test]
fn infeasible_sibling_schedule_ends_as_infeasible_path() {
    // assert(true)'s failing sibling is unsatisfiable: it must show up as an
    // infeasible path, not as a failure and not be silently dropped.
    let report = analyze_src(
        r#"
        param x: u8;
        assert(x == x);
        "#,
    );
    assert_eq!(report.verdict, Verdict::Safe);
    assert_eq!(report.path_counts.safe, 1);
    assert_eq!(report.path_counts.infeasible, 1);
    assert_eq!(report.path_counts.failed, 0);
}

#[test]
fn assume_false_makes_whole_program_infeasible() {
    let report = analyze_src(
        r#"
        param x: u8;
        assume(x != x);
        assert(false, "nothing is reachable");
        "#,
    );
    // No path exists at all: not safe (nothing verified), not unsafe.
    // The single run dies at the assume, so there is no unknown remainder;
    // with zero feasible paths and zero failures the verdict is safe only
    // if exploration was complete — here it was, and nothing was violated.
    assert_eq!(report.path_counts.failed, 0);
    assert_eq!(report.path_counts.safe, 0);
    assert!(matches!(report.verdict, Verdict::Safe | Verdict::Unknown));
}

#[test]
fn unreachable_after_contradictory_assumes() {
    let report = analyze_src(
        r#"
        param x: u8;
        assume(x < 5u8);
        assume(x > 10u8);
        assert(false);
        "#,
    );
    assert_eq!(report.path_counts.failed, 0);
    assert_eq!(report.path_counts.infeasible, 1);
}
