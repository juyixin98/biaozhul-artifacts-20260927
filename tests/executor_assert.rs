//! Assertion-failure detection: the engine must find the failing inputs,
//! report the right failure site, and the counterexample must reproduce.

mod common;

use common::*;
use symex::kernel::report::{FailureKind, Verdict};

#[test]
fn detects_assertion_failure_with_exact_site() {
    // Node ids are assigned in DFS pre-order after parsing:
    //   stmt0 let (id 0), Bin+ (1), var x (2), lit (3)
    //   stmt1 assert (id 4), Bin!= (5), var y (6), lit (7)
    let report = analyze_src(
        r#"
        param x: u8;
        let y: u8 = x + 1u8;
        assert(y != 0u8, "increment must not wrap to zero");
        "#,
    );
    assert_eq!(report.verdict, Verdict::Unsafe);
    assert_eq!(report.findings.len(), 1);
    let f = &report.findings[0];
    assert_eq!(f.kind, FailureKind::AssertionFailed);
    assert_eq!(f.node_id, 4, "assert statement id");
    assert_eq!(f.message.as_deref(), Some("increment must not wrap to zero"));
    // The only input with (x+1) mod 256 == 0 is 255.
    assert_eq!(f.counterexample.get("x"), Some(&255));
    assert!(f.replay.reproduced(), "counterexample must replay");
}

#[test]
fn counterexample_reaches_same_failure_line() {
    let report = analyze_src(
        r#"
        param x: u8;
        let y: u8 = x * 2u8;
        assert(y >= x, "doubling cannot shrink");
        "#,
    );
    assert_eq!(report.verdict, Verdict::Unsafe);
    let f = &report.findings[0];
    assert_eq!(f.line, 4, "assert is on source line 4");
    let x = f.counterexample["x"];
    assert!(x >= 128, "doubling wraps exactly for x >= 128, got {x}");
    assert!(f.replay.reproduced());
    // The replay's recorded failure must be the same node the kernel blamed.
    let site = f.replay.replay_failure.as_ref().expect("replay failure site");
    assert_eq!(site.node_id, f.node_id);
}

#[test]
fn holds_when_assertion_is_valid() {
    let report = analyze_src(
        r#"
        param x: u8;
        let y: u8 = x & 15u8;
        assert(y < 16u8);
        "#,
    );
    assert_eq!(report.verdict, Verdict::Safe);
    assert!(report.findings.is_empty());
    assert_eq!(report.path_counts.failed, 0);
}

#[test]
fn division_by_zero_is_a_failure_not_an_exception() {
    // stmt0 let (0), Bin/ (1), var a (2), var b (3); stmt1 assert (4), ...
    let report = analyze_src(
        r#"
        param a: u8;
        param b: u8;
        let c: u8 = a / b;
        assert(c <= a);
        "#,
    );
    assert_eq!(report.verdict, Verdict::Unsafe);
    let f = report
        .findings
        .iter()
        .find(|f| f.kind == FailureKind::DivisionByZero)
        .expect("division-by-zero finding");
    assert_eq!(f.node_id, 3, "failure is attributed to the divisor expression");
    assert_eq!(f.counterexample.get("b"), Some(&0));
    assert!(f.replay.reproduced());
}
