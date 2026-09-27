//! Integer wraparound: fixed-width overflow semantics must be exactly the
//! machine semantics, and the counterexamples must be the boundary values.

mod common;

use common::*;
use symex::kernel::report::Verdict;

#[test]
fn addition_wrap_to_zero_is_found() {
    let report = analyze_src(
        r#"
        param x: u8;
        let y: u8 = x + 1u8;
        assert(y != 0u8);
        "#,
    );
    assert_eq!(report.verdict, Verdict::Unsafe);
    assert_eq!(report.findings[0].counterexample.get("x"), Some(&255));
}

#[test]
fn subtraction_underflow_is_found() {
    let report = analyze_src(
        r#"
        param x: u8;
        let y: u8 = x - 1u8;
        assert(y <= x, "decrement cannot increase");
        "#,
    );
    assert_eq!(report.verdict, Verdict::Unsafe);
    // y = x-1 wraps to 255 only when x == 0.
    assert_eq!(report.findings[0].counterexample.get("x"), Some(&0));
    assert!(report.findings[0].replay.reproduced());
}

#[test]
fn multiplication_overflow_boundary() {
    // x*x overflows u8 exactly when x >= 16 (16*16 = 256 == 0 mod 256).
    let report = analyze_src(
        r#"
        param x: u8;
        let y: u8 = x * x;
        assert(y >= x, "square cannot shrink below x for x>1");
        "#,
    );
    assert_eq!(report.verdict, Verdict::Unsafe);
    let x = report.findings[0].counterexample["x"];
    // Smallest witness: 16 (16*16 wraps to 0 < 16). The solver may return any
    // witness, but every witness must satisfy the wrapped predicate.
    assert!(((x as u64 * x as u64) & 0xff) < x as u64 || x <= 1);
    assert!(report.findings[0].replay.reproduced());
}

#[test]
fn no_wrap_means_safe() {
    // x <= 100 so x + 100 <= 200 < 256: no wraparound possible.
    let report = analyze_src(
        r#"
        param x: u8;
        assume(x <= 100u8);
        let y: u8 = x + 100u8;
        assert(y >= x);
        "#,
    );
    assert_eq!(report.verdict, Verdict::Safe);
}

#[test]
fn wraparound_semantics_match_concrete_machine() {
    // 16-bit: 65535 + 1 == 0 must be found, and the witness must be 65535.
    let report = analyze_src(
        r#"
        param x: u16;
        let y: u16 = x + 1u16;
        assert(y != 0u16);
        "#,
    );
    assert_eq!(report.verdict, Verdict::Unsafe);
    assert_eq!(report.findings[0].counterexample.get("x"), Some(&65535));
}
