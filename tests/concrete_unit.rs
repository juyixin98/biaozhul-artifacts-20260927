//! Concrete interpreter tests with hand-computed outcomes. Each test asserts
//! both the result value(s) and the exact failure category — never merely that
//! an interface could be called.

use interval_analyzer::concrete::{run, ConcErrorKind};
use interval_analyzer::lang::parse;
use std::collections::BTreeMap;

fn inputs(pairs: &[(&str, i64)]) -> BTreeMap<String, i64> {
    pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
}

fn err(src: &str, env: &BTreeMap<String, i64>) -> ConcErrorKind {
    let prog = parse(src).expect("parse");
    run(&prog, env, 100_000)
        .error
        .expect("expected an error")
        .kind
}

#[test]
fn bounded_addition_overflow_is_detected_not_wrapped() {
    let src = "let x: [0, 1]; y := x + 9223372036854775807;";
    let k = err(src, &inputs(&[("x", 1)]));
    assert!(matches!(k, ConcErrorKind::Overflow), "got {k:?}");
    // x = 0 fits: no error, exact value preserved
    let prog = parse(src).unwrap();
    let out = run(&prog, &inputs(&[("x", 0)]), 100_000);
    assert!(out.error.is_none());
    assert_eq!(out.final_vars["y"], i64::MAX);
}

#[test]
fn negation_of_i64_min_overflows() {
    // Construct i64::MIN without writing its positive literal: 0-MAX-1 = MIN,
    // then negating MIN must trap instead of wrapping.
    let src = "x := 0 - 9223372036854775807 - 1; y := 0 - x;";
    let prog = parse(src).unwrap();
    let out = run(&prog, &inputs(&[]), 100_000);
    assert_eq!(out.final_vars["x"], i64::MIN);
    let k = out.error.expect("expected overflow on -MIN").kind;
    assert!(matches!(k, ConcErrorKind::Overflow), "got {k:?}");
}

#[test]
fn array_index_failure_categories() {
    let src = "let i: [0, 5]; array a[3]; x := a[i];";
    let prog = parse(src).unwrap();
    // in bounds
    let ok = run(&prog, &inputs(&[("i", 2)]), 100_000);
    assert!(ok.error.is_none());
    // above the last index
    match run(&prog, &inputs(&[("i", 3)]), 100_000)
        .error
        .unwrap()
        .kind
    {
        ConcErrorKind::OutOfBounds { index, len } => {
            assert_eq!(index, 3);
            assert_eq!(len, 3);
        }
        other => panic!("expected OOB, got {other:?}"),
    }
    // negative index
    let src2 = "array a[3]; j := 0 - 1; x := a[j];";
    let prog2 = parse(src2).unwrap();
    let k = run(&prog2, &inputs(&[]), 100_000).error.unwrap().kind;
    assert!(
        matches!(k, ConcErrorKind::OutOfBounds { index: -1, len: 3 }),
        "got {k:?}"
    );
}

#[test]
fn assertion_failure_is_its_own_category() {
    let src = "let x: [0, 1]; assert x == 0;";
    let prog = parse(src).unwrap();
    assert!(run(&prog, &inputs(&[("x", 0)]), 100_000).error.is_none());
    let k = run(&prog, &inputs(&[("x", 1)]), 100_000)
        .error
        .unwrap()
        .kind;
    assert!(matches!(k, ConcErrorKind::AssertFailed), "got {k:?}");
}

#[test]
fn loop_accumulates_exact_sum() {
    // sum 0+1+2 for n=3, i ends at 3
    let src = "\
let n: [0, 5];
i := 0;
s := 0;
while i < n {
  s := s + i;
  i := i + 1;
}";
    let prog = parse(src).unwrap();
    let out = run(&prog, &inputs(&[("n", 3)]), 100_000);
    assert!(out.error.is_none(), "{:?}", out.error);
    assert_eq!(out.final_vars["i"], 3);
    assert_eq!(out.final_vars["s"], 3);

    // n = 0: zero iterations
    let out0 = run(&prog, &inputs(&[("n", 0)]), 100_000);
    assert_eq!(out0.final_vars["i"], 0);
    assert_eq!(out0.final_vars["s"], 0);
}

#[test]
fn fuel_caps_non_terminating_loops() {
    // classic divergent loop under bounded execution
    let src = "i := 0; while i >= 0 { i := i + 1; }";
    let prog = parse(src).unwrap();
    let k = run(&prog, &inputs(&[]), 50).error.unwrap().kind;
    assert!(matches!(k, ConcErrorKind::FuelExhausted), "got {k:?}");
}
