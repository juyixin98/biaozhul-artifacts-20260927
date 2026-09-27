//! Exploration budgets: loop unrolling caps and path budgets must surface as
//! `unknown`, never as `safe`, and the report must echo the budgets used.

mod common;

use common::*;
use symex::config::EngineConfig;
use symex::kernel::report::{PathStatus, Verdict};

fn cfg(max_paths: u32, loop_unroll: u32) -> EngineConfig {
    EngineConfig {
        max_paths,
        loop_unroll,
        solver_timeout_ms: 5_000,
    }
}

#[test]
fn loop_beyond_unroll_bound_is_unknown_not_safe() {
    let report = analyze_src_cfg(
        r#"
        param x: u8;
        let i: u8 = 0u8;
        while (i < x) {
            i = i + 1u8;
        }
        assert(i >= 0u8);
        "#,
        &cfg(512, 4),
    );
    assert_eq!(report.verdict, Verdict::Unknown);
    assert!(report.path_counts.incomplete_unroll > 0);
    assert!(report
        .paths
        .iter()
        .any(|p| p.status == PathStatus::IncompleteUnroll));
    // The budgets actually used are echoed back.
    assert_eq!(report.engine.loop_unroll, 4);
    assert_eq!(report.engine.max_paths, 512);
}

#[test]
fn loop_within_unroll_bound_is_safe() {
    // x is assumed small, so the loop terminates within the bound on every
    // feasible path.
    let report = analyze_src_cfg(
        r#"
        param x: u8;
        assume(x <= 3u8);
        let i: u8 = 0u8;
        while (i < x) {
            i = i + 1u8;
        }
        assert(i <= 3u8);
        "#,
        &cfg(512, 8),
    );
    assert_eq!(report.verdict, Verdict::Safe);
    assert_eq!(report.path_counts.incomplete_unroll, 0);
}

#[test]
fn path_budget_exhaustion_is_reported_as_truncation() {
    let report = analyze_src_cfg(
        r#"
        param x: u8;
        if (x < 1u8) { let a: u8 = 1u8; }
        if (x < 2u8) { let b: u8 = 1u8; }
        if (x < 3u8) { let c: u8 = 1u8; }
        if (x < 4u8) { let d: u8 = 1u8; }
        assert(x >= 0u8);
        "#,
        &cfg(2, 8),
    );
    assert_eq!(report.verdict, Verdict::Unknown);
    assert!(report.budget.truncated);
    assert!(report.budget.truncation_reason.is_some());
    assert_eq!(report.budget.paths_explored, 2);
    assert!(report.path_counts.incomplete_budget >= 1);
}

#[test]
fn budgets_are_echoed_in_result() {
    let report = analyze_src_cfg(
        r#"
        param x: u8;
        assert(x == x);
        "#,
        &cfg(7, 3),
    );
    assert_eq!(report.engine.max_paths, 7);
    assert_eq!(report.engine.loop_unroll, 3);
    assert_eq!(report.budget.max_paths, 7);
    assert!(!report.budget.truncated);
}
