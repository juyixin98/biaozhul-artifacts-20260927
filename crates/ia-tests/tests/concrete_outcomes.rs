//! Tests asserting *concrete* outcomes and exact failure categories, with the
//! expected answers written by hand (not produced by the solver). These pin
//! the reference semantics independently of the abstract implementation.
#[path = "common/mod.rs"]
mod common;

use common::{compile, fixture};
use ia_concrete::{run_program, FailureKind, RunOutcome};
use std::collections::HashMap;

fn assign(pairs: &[(&str, i64)]) -> HashMap<String, i64> {
    pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
}

fn failure_of(out: RunOutcome) -> FailureKind {
    match out {
        RunOutcome::Failed { failure, .. } => failure.kind,
        other => panic!("expected failure, got {other:?}"),
    }
}

fn var_of(out: RunOutcome, name: &str) -> i64 {
    match out {
        RunOutcome::Normal { final_vars, .. } => final_vars[name],
        other => panic!("expected normal, got {other:?}"),
    }
}

#[test]
fn loop_growth_concrete_executions() {
    let src = fixture("01_loop_growth.ial");
    let (program, info) = compile(&src);
    // n = 0: body never runs, y = a[0] = 0, x stays 0.
    let out = run_program(&program, &info, &src, &assign(&[("n", 0)]));
    assert_eq!(var_of(out, "x"), 0);
    let out = run_program(&program, &info, &src, &assign(&[("n", 0)]));
    assert_eq!(var_of(out, "y"), 0);
    // n = 5: x ends at 5 (the program writes a[4] = 8 internally).
    let out = run_program(&program, &info, &src, &assign(&[("n", 5)]));
    assert_eq!(var_of(out, "x"), 5);
    // n = 3: loop stops with x == 3.
    let out = run_program(&program, &info, &src, &assign(&[("n", 3)]));
    assert_eq!(var_of(out, "x"), 3);
    // Every input 0..=5 must terminate normally.
    for n in 0..=5 {
        let out = run_program(&program, &info, &src, &assign(&[("n", n)]));
        assert!(matches!(out, RunOutcome::Normal { .. }), "n={n} should be normal");
    }
}

#[test]
fn branch_refinement_concrete_oob() {
    let src = fixture("02_branch_refine.ial");
    let (program, info) = compile(&src);
    // i = -1: guarded read skipped, unguarded `z = a[i]` OOB.
    let out = run_program(&program, &info, &src, &assign(&[("i", -1)]));
    assert_eq!(failure_of(out), FailureKind::IndexOutOfBounds);
    // i = 10: unguarded read OOB.
    let out = run_program(&program, &info, &src, &assign(&[("i", 10)]));
    assert_eq!(failure_of(out), FailureKind::IndexOutOfBounds);
    // i = 0..=3: all reads in bounds, normal.
    for i in 0..=3 {
        let out = run_program(&program, &info, &src, &assign(&[("i", i)]));
        assert!(matches!(out, RunOutcome::Normal { .. }), "i={i} should be normal");
    }
}

#[test]
fn overflow_paths_concrete_categories() {
    let src = fixture("03_overflow_paths.ial");
    let (program, info) = compile(&src);

    // x = MAX: x + 100 overflows.
    let out = run_program(
        &program,
        &info,
        &src,
        &assign(&[("x", i64::MAX), ("mn", i64::MIN + 8), ("w", 1)]),
    );
    assert_eq!(failure_of(out), FailureKind::Overflow);

    // A fully safe path cannot use the shared fixture (it always ends on the
    // guaranteed MIN/-1 overflow), so exercise the intermediate safe choices
    // on a stripped program: x + 0 never overflows and w + 3 is a safe divisor.
    let safe_prog =
        "input x [9223372036854775607: 9223372036854775807];\ninput w [-2: 2];\n{\na = x + 0;\ns = 100 / (w + 3);\n}\n";
    let (sp, si) = compile(safe_prog);
    let out = run_program(
        &sp,
        &si,
        safe_prog,
        &assign(&[("x", i64::MAX - 200), ("w", 1)]),
    );
    assert!(matches!(out, RunOutcome::Normal { .. }), "expected normal: {out:?}");

    // mn = MIN: -mn overflows.
    let out = run_program(
        &program,
        &info,
        &src,
        &assign(&[("x", i64::MAX - 200), ("mn", i64::MIN), ("w", 1)]),
    );
    assert_eq!(failure_of(out), FailureKind::Overflow);

    // w = 0: 10 / 0 div by zero (reached before -mn check ordering).
    let out = run_program(
        &program,
        &info,
        &src,
        &assign(&[("x", i64::MAX - 200), ("mn", i64::MIN + 8), ("w", 0)]),
    );
    assert_eq!(failure_of(out), FailureKind::DivByZero);
}

#[test]
fn literal_min_div_minus_one_is_overflow_when_reached() {
    // The guaranteed site is last in the fixture; reach it with otherwise-safe
    // values to confirm the concrete category is Overflow, not DivByZero.
    let src = "input w [1: 1]; {\nx = 10 / w;\ny = (-9223372036854775808) / (-1);\n}\n";
    let (program, info) = compile(src);
    let out = run_program(&program, &info, src, &assign(&[("w", 1)]));
    assert_eq!(failure_of(out), FailureKind::Overflow);
}

#[test]
fn countdown_concrete_exit_is_minus_one() {
    let src = fixture("06_countdown.ial");
    let (program, info) = compile(&src);
    for start in 0..=6 {
        let out = run_program(&program, &info, &src, &assign(&[("start", start)]));
        assert_eq!(var_of(out, "i"), -1, "start={start} should leave i == -1");
    }
}

#[test]
fn mod_and_assert_concrete() {
    let src = fixture("05_mod_assert.ial");
    let (program, info) = compile(&src);
    // m = -7: assert(m > -3) fails.
    let out = run_program(&program, &info, &src, &assign(&[("m", -7)]));
    assert_eq!(failure_of(out), FailureKind::AssertionFailed);
    // m = -2: passes.
    let out = run_program(&program, &info, &src, &assign(&[("m", -2)]));
    assert!(matches!(out, RunOutcome::Normal { .. }));
    // Truncated modulo semantics toward zero.
    let out = run_program(&program, &info, &src, &assign(&[("m", -7)]));
    let _ = out; // expected failure, covered above
    let prog = "input m [-7: 7]; { r = m % 3; }\n";
    let (p2, i2) = compile(prog);
    let out = run_program(&p2, &i2, prog, &assign(&[("m", -7)]));
    assert_eq!(var_of(out, "r"), -1); // -7 % 3 == -1 (toward zero)
    let out = run_program(&p2, &i2, prog, &assign(&[("m", 7)]));
    assert_eq!(var_of(out, "r"), 1);
}

#[test]
fn concrete_step_limit_is_a_distinct_category() {
    // A program that runs forever must surface StepLimit, not hang and not be
    // mistaken for normal.
    let src = "input x [1: 1]; {\nwhile (x > 0) { skip; }\n}\n";
    let (program, info) = compile(src);
    let out = ia_concrete::run_program_bounded(
        &program,
        &info,
        src,
        &assign(&[("x", 1)]),
        100,
    );
    assert_eq!(failure_of(out), FailureKind::StepLimit);
}
