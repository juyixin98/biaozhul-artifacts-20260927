//! Replay verification: counterexamples must reproduce through the
//! independent concrete interpreter, and tampered inputs must not.

mod common;

use common::*;
use symex::evidence::concrete::{
    run as run_concrete, ConcreteInput, ConcreteOutcome, FailureKind as ConcKind,
};
use symex::evidence::replay::{replay_counterexample, ReplayStatus};
use symex::kernel::report::Verdict;

const WRAP_SRC: &str = r#"
    param x: u8;
    let y: u8 = x + 1u8;
    assert(y != 0u8);
"#;

#[test]
fn reported_counterexample_replays_to_same_site() {
    let report = analyze_src(WRAP_SRC);
    let f = &report.findings[0];
    assert_eq!(f.replay.status, ReplayStatus::Reproduced);
    let site = f.replay.replay_failure.as_ref().unwrap();
    assert_eq!(site.kind, ConcKind::AssertionFailed);
    assert_eq!(site.node_id, f.node_id);
}

#[test]
fn tampered_input_does_not_reproduce() {
    let report = analyze_src(WRAP_SRC);
    let f = &report.findings[0];
    let program = prog(WRAP_SRC);
    // 254 does not wrap: replay must NOT confirm the failure.
    let mut bad = ConcreteInput::new();
    bad.insert("x".to_string(), 254u64);
    let check = replay_counterexample(&program, ConcKind::AssertionFailed, f.node_id, &bad);
    assert_eq!(check.status, ReplayStatus::NoFailure);
    assert!(!check.reproduced());
}

#[test]
fn concrete_interpreter_classifies_outcomes() {
    // Completed
    let p = prog("param x: u8; assert(x == x);");
    let mut i = ConcreteInput::new();
    i.insert("x".to_string(), 7u64);
    let r = run_concrete(&p, &i).unwrap();
    assert_eq!(r.outcome, ConcreteOutcome::Completed);
    assert!(r.failure.is_none());

    // AssertionFailed with the right node id (the assert statement itself
    // is node 0; its condition and operands follow).
    let p = prog("param x: u8; assert(x > 10u8);");
    let mut i = ConcreteInput::new();
    i.insert("x".to_string(), 3u64);
    let r = run_concrete(&p, &i).unwrap();
    assert_eq!(r.outcome, ConcreteOutcome::Failed);
    let site = r.failure.unwrap();
    assert_eq!(site.kind, ConcKind::AssertionFailed);
    assert_eq!(site.node_id, 0);

    // DivisionByZero attributed to the divisor expression.
    // stmt0 let (0), Bin/ (1), var a (2), var b (3).
    let p = prog("param a: u8; param b: u8; let c: u8 = a / b;");
    let mut i = ConcreteInput::new();
    i.insert("a".to_string(), 5u64);
    i.insert("b".to_string(), 0u64);
    let r = run_concrete(&p, &i).unwrap();
    assert_eq!(r.outcome, ConcreteOutcome::Failed);
    let site = r.failure.unwrap();
    assert_eq!(site.kind, ConcKind::DivisionByZero);
    assert_eq!(site.node_id, 3);
}

#[test]
fn concrete_interpreter_records_branch_trace() {
    let p = prog(
        r#"
        param x: u8;
        if (x > 5u8) {
            assert(x > 5u8);
        }
        "#,
    );
    let mut i = ConcreteInput::new();
    i.insert("x".to_string(), 9u64);
    let r = run_concrete(&p, &i).unwrap();
    assert_eq!(r.steps.len(), 2, "one branch step + one assert step");
    assert_eq!(r.steps[0].taken, Some(true));
    assert_eq!(r.steps[1].passed, Some(true));
}

#[test]
fn concrete_input_validation() {
    let p = prog("param x: u8; assert(x == x);");
    // missing input
    let r = run_concrete(&p, &ConcreteInput::new());
    assert!(r.is_err());
    // out of range
    let mut i = ConcreteInput::new();
    i.insert("x".to_string(), 256u64);
    assert!(run_concrete(&p, &i).is_err());
    // extra input
    let mut i = ConcreteInput::new();
    i.insert("x".to_string(), 1u64);
    i.insert("z".to_string(), 1u64);
    assert!(run_concrete(&p, &i).is_err());
}

#[test]
fn unsafe_verdict_implies_reproduced_replay() {
    // Across all local fixtures, every unsafe verdict must carry a replayed
    // counterexample — the "unknown/exception never reported as success"
    // invariant.
    for src in [
        WRAP_SRC,
        "param x: u8; let y: u8 = x - 1u8; assert(y <= x);",
        "param a: u8; param b: u8; let c: u8 = a / b; assert(c <= a);",
    ] {
        let report = analyze_src(src);
        assert_eq!(report.verdict, Verdict::Unsafe);
        assert!(report.findings.iter().all(|f| f.replay.reproduced()));
    }
}
