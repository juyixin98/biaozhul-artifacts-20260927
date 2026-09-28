//! Evidence-verifier tests: a genuine report verifies; tampering with a
//! verdict, an invariant or the source binding must be rejected.

use interval_analyzer::evidence::verify;
use interval_analyzer::kernel::{AbsState, Interval};
use interval_analyzer::lang::parse;
use interval_analyzer::report::{CheckKind, Evidence, Verdict};
use interval_analyzer::{analyze_source, config::Config};

#[test]
fn fresh_report_verifies() {
    let src = "\
let n: [0, 6];
i := 0;
while i < n {
  i := i + 1;
}
assert i >= 0;";
    let prog = parse(src).unwrap();
    let report = analyze_source(src, Config::default()).unwrap();
    let result = verify(src, &prog, &report);
    assert!(
        result.ok,
        "fresh report should verify: {:#?}",
        result.failures
    );
    assert_eq!(result.checked_invariants, 1);
    assert!(result.checked_checks >= 2);
}

#[test]
fn tampered_verdict_is_rejected() {
    let src = "let x: [0, 1]; y := x + 1;";
    let prog = parse(src).unwrap();
    let mut report = analyze_source(src, Config::default()).unwrap();
    assert!(!report.checks.is_empty());
    // forge the first check into a violation without changing its evidence
    report.checks[0].verdict = Verdict::Violated;
    let result = verify(src, &prog, &report);
    assert!(!result.ok);
    assert!(
        result
            .failures
            .iter()
            .any(|f| f.location.starts_with("check[0]")),
        "{:?}",
        result.failures
    );
}

#[test]
fn verdict_unsupported_by_invariants_is_rejected() {
    // Keep evidence consistent (verdict follows from evidence) but widen an
    // exit state, then the re-derived checks still match verdicts; instead we
    // shrink a loop invariant so it stops being a post-fixpoint.
    let src = "\
let n: [0, 6];
i := 0;
while i < n {
  i := i + 1;
}";
    let prog = parse(src).unwrap();
    let mut report = analyze_source(src, Config::default()).unwrap();
    let inv = &mut report.loop_invariants[0];
    // claim i is always exactly 0 at the head — contradicted by i := i+1
    let mut m = std::collections::BTreeMap::new();
    for (k, v) in &inv.invariant.vars {
        if k == "i" {
            m.insert(k.clone(), Interval::constant(0));
        } else {
            m.insert(k.clone(), *v);
        }
    }
    inv.invariant = AbsState {
        vars: m,
        arrays: inv.invariant.arrays.clone(),
        is_bottom: false,
    };
    let result = verify(src, &prog, &report);
    assert!(!result.ok);
    assert!(
        result
            .failures
            .iter()
            .any(|f| f.location.contains("loop_invariant") && f.message.contains("post-fixpoint")),
        "{:?}",
        result.failures
    );
}

#[test]
fn mismatched_source_hash_is_rejected() {
    let src = "let x: [0, 1]; y := x + 1;";
    let prog = parse(src).unwrap();
    let mut report = analyze_source(src, Config::default()).unwrap();
    report.program_hash = "deadbeefdeadbeef".into();
    let result = verify(src, &prog, &report);
    assert!(!result.ok);
    assert!(result
        .failures
        .iter()
        .any(|f| f.location == "report.program_hash"));
}

#[test]
fn evidence_verdict_is_a_pure_function_of_kind_and_evidence() {
    // index [0,20] vs [0,9] must be maybe, never definite
    let ev = Evidence::ArrayIndex {
        index: Interval::finite(0, 20),
        valid_lo: 0,
        valid_hi: 9,
        array_len: 10,
    };
    assert_eq!(
        interval_analyzer::report::verdict_of(CheckKind::ArrayIndex, &ev),
        Verdict::MaybeViolated
    );
    // index [-5,-5] vs [0,9] definite
    let ev2 = Evidence::ArrayIndex {
        index: Interval::constant(-5),
        valid_lo: 0,
        valid_hi: 9,
        array_len: 10,
    };
    assert_eq!(
        interval_analyzer::report::verdict_of(CheckKind::ArrayIndex, &ev2),
        Verdict::Violated
    );
    // an Assert check with a forged "unreachable" evidence pair is reported as
    // unreachable, not silently swallowed
    let ev3 = Evidence::Assert {
        true_feasible: false,
        false_feasible: false,
    };
    assert_eq!(
        interval_analyzer::report::verdict_of(CheckKind::Assert, &ev3),
        Verdict::Unreachable
    );
}
