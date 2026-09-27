//! Differential soundness tests: for every fixture with a small declared
//! input domain, enumerate ALL concrete inputs with the INDEPENDENT concrete
//! executor and assert that every concrete outcome is accounted for by the
//! abstract report.
//!
//! The reference answers here are produced by `ia-concrete` — a separate
//! implementation that shares no solver code — never by the core under test.
#[path = "common/mod.rs"]
mod common;

use common::{compile, fixture};
use ia_concrete::{enumerate_inputs, run_program, FailureKind, RunOutcome};
use ia_solver::{analyze, AnalyzerConfig, CheckKind, CheckVerdict};
use ia_verify::{verify, VerifyConfig};
use std::collections::{BTreeMap, BTreeSet};

fn expected_kind(k: CheckKind) -> FailureKind {
    match k {
        CheckKind::Overflow => FailureKind::Overflow,
        CheckKind::DivByZero => FailureKind::DivByZero,
        CheckKind::IndexBounds => FailureKind::IndexOutOfBounds,
        CheckKind::Assertion => FailureKind::AssertionFailed,
    }
}

/// Exhaustive core property: every concrete failure lands on an abstract site
/// admitting failure; every concrete normal value is inside the abstract exit
/// interval; nothing concrete is marked unreachable.
fn assert_soundness(source: &str, narrowing: bool) {
    let (program, info) = compile(source);
    let cfg = AnalyzerConfig {
        narrowing,
        ..Default::default()
    };
    let report = analyze(&program, &info, cfg);
    let assignments = enumerate_inputs(&info, 1_000_000).expect("fixture domain must be enumerable");

    // Index abstract sites by (offset, kind).
    let mut by_site: BTreeMap<(usize, CheckKind), CheckVerdict> = BTreeMap::new();
    for c in &report.checks {
        by_site.insert((c.span.start.offset, c.kind), c.verdict);
    }

    let mut concrete_exit: BTreeMap<String, ia_intervals::Interval> = BTreeMap::new();
    let mut concrete_failures: BTreeSet<(usize, FailureKind)> = BTreeSet::new();

    for assignment in &assignments {
        match run_program(&program, &info, source, assignment) {
            RunOutcome::Normal { final_vars, .. } => {
                for (k, v) in final_vars {
                    let e = concrete_exit
                        .entry(k)
                        .or_insert(ia_intervals::Interval::BOTTOM);
                    *e = e.join(ia_intervals::Interval::point(v));
                }
            }
            RunOutcome::Failed { failure, .. } => {
                concrete_failures.insert((failure.at_offset, failure.kind));
                // Find the matching abstract check at this exact offset with
                // the matching kind.
                let kind = match failure.kind {
                    FailureKind::Overflow => CheckKind::Overflow,
                    FailureKind::DivByZero => CheckKind::DivByZero,
                    FailureKind::IndexOutOfBounds => CheckKind::IndexBounds,
                    FailureKind::AssertionFailed => CheckKind::Assertion,
                    FailureKind::StepLimit => panic!("concrete step limit in fixture"),
                };
                let verdict = by_site
                    .get(&(failure.at_offset, kind))
                    .unwrap_or_else(|| {
                        panic!(
                            "concrete {:?} at offset {} has no abstract check of that kind",
                            failure.kind, failure.at_offset
                        )
                    });
                assert!(
                    matches!(
                        verdict,
                        CheckVerdict::PossibleFailure | CheckVerdict::GuaranteedFailure
                    ),
                    "concrete {:?} observed at safe/unreachable site (offset {})",
                    failure.kind,
                    failure.at_offset
                );
            }
        }
    }

    // Every abstract guaranteed-failure site must be concretely observed.
    for c in &report.checks {
        if c.verdict == CheckVerdict::GuaranteedFailure {
            let fk = expected_kind(c.kind);
            assert!(
                concrete_failures.contains(&(c.span.start.offset, fk)),
                "abstract guaranteed-failure at {} offset {} never concretely observed",
                c.kind.as_str(),
                c.span.start.offset
            );
        }
        if c.verdict == CheckVerdict::Unreachable {
            for k in [
                FailureKind::Overflow,
                FailureKind::DivByZero,
                FailureKind::IndexOutOfBounds,
                FailureKind::AssertionFailed,
            ] {
                assert!(
                    !concrete_failures.contains(&(c.span.start.offset, k)),
                    "site marked unreachable was concretely reached"
                );
            }
        }
    }

    // Exit-value containment against a fresh solver run's final state.
    let exit = ia_solver::analyzer::exit_intervals(
        &program,
        &info,
        AnalyzerConfig {
            narrowing,
            ..Default::default()
        },
    );
    for (name, concrete_iv) in &concrete_exit {
        let abs_iv = exit
            .get(name)
            .unwrap_or_else(|| panic!("abstract exit missing variable {name}"));
        assert!(
            concrete_iv.subset_of(*abs_iv),
            "concrete exit {concrete_iv:?} for `{name}` not contained in abstract {abs_iv:?}"
        );
    }
}

#[test]
fn loop_growth_is_sound_with_and_without_narrowing() {
    let src = fixture("01_loop_growth.ial");
    assert_soundness(&src, true);
    assert_soundness(&src, false);
}

#[test]
fn branch_refinement_is_sound() {
    assert_soundness(&fixture("02_branch_refine.ial"), true);
}

#[test]
fn overflow_paths_are_sound() {
    assert_soundness(&fixture("03_overflow_paths.ial"), true);
}

#[test]
fn unreachable_fixture_is_sound() {
    assert_soundness(&fixture("04_unreachable.ial"), true);
}

#[test]
fn mod_and_assert_is_sound() {
    assert_soundness(&fixture("05_mod_assert.ial"), true);
}

#[test]
fn countdown_loop_is_sound() {
    let src = fixture("06_countdown.ial");
    assert_soundness(&src, true);
    assert_soundness(&src, false);
}

#[test]
fn verifier_reports_sound_for_every_fixture() {
    for name in [
        "01_loop_growth.ial",
        "02_branch_refine.ial",
        "03_overflow_paths.ial",
        "04_unreachable.ial",
        "05_mod_assert.ial",
        "06_countdown.ial",
    ] {
        let src = fixture(name);
        let (program, info) = compile(&src);
        let report = verify(&src, &program, &info, &VerifyConfig::default())
            .unwrap_or_else(|e| panic!("{name} enumeration failed: {e:?}"));
        assert!(report.enumeration_complete, "{name} should fully enumerate");
        assert!(report.sound, "{name} verifier violations: {:?}", report.violations);
    }
}
