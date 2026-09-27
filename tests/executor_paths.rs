//! Mutually exclusive branches: both sides must be explored, and the path
//! records must show both decisions actually happened.

mod common;

use common::*;
use symex::kernel::report::{PathStatus, Verdict};

#[test]
fn mutually_exclusive_branches_are_both_explored() {
    let report = analyze_src(
        r#"
        param x: u8;
        if (x < 128u8) {
            assert(x < 128u8);
        } else {
            assert(x >= 128u8);
        }
        "#,
    );
    assert_eq!(report.verdict, Verdict::Safe);
    // Two real leaves (then / else); the assert-failure siblings are
    // infeasible because each branch condition matches its assertion.
    assert_eq!(report.path_counts.safe, 2);
    assert_eq!(report.path_counts.infeasible, 2);
    assert_eq!(report.path_counts.failed, 0);

    // The two safe paths must take opposite decisions at the `if`.
    let safe: Vec<_> = report
        .paths
        .iter()
        .filter(|p| p.status == PathStatus::Safe)
        .collect();
    let taken: Vec<bool> = safe
        .iter()
        .map(|p| p.branches[0].taken)
        .collect();
    assert!(taken.contains(&true), "then-branch path exists");
    assert!(taken.contains(&false), "else-branch path exists");
}

#[test]
fn nested_exclusive_branches_produce_four_paths() {
    let report = analyze_src(
        r#"
        param x: u8;
        if (x < 64u8) {
            if (x < 32u8) {
                assert(x < 32u8);
            } else {
                assert(x >= 32u8);
            }
        } else {
            assert(x >= 64u8);
        }
        "#,
    );
    assert_eq!(report.verdict, Verdict::Safe);
    // Leaves: x<32, 32<=x<64, x>=64 — three safe paths.
    assert_eq!(report.path_counts.safe, 3);
    assert_eq!(report.path_counts.failed, 0);
}
