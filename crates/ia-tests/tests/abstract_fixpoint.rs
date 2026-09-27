//! Tests that pin abstract-domain and fixpoint behaviour directly:
//! widening/narrowing iteration counts, interval containment, and the
//! safe/possible/guaranteed/unreachable verdicts on targeted programs.
#[path = "common/mod.rs"]
mod common;

use common::{compile, fixture};
use ia_intervals::Interval;
use ia_solver::{analyze, AnalyzerConfig, CheckVerdict};

fn find(
    report: &ia_solver::AnalysisReport,
    line: u32,
    kind: ia_solver::CheckKind,
) -> &ia_solver::CheckRecord {
    report
        .checks
        .iter()
        .find(|c| c.span.start.line == line && c.kind == kind)
        .unwrap_or_else(|| panic!("no {kind:?} check on line {line}"))
}

#[test]
fn widen_then_narrow_bounds_induction_variable() {
    // Widening alone jumps x's upper endpoint to i64::MAX; after narrowing the
    // guard x < 6 cuts it back to [0, 5].
    let src = "input n [0: 5]; {\nx = 0;\nwhile (x < n) { x = x + 1; }\n}\n";
    let (program, info) = compile(src);

    let narrowed = analyze(
        &program,
        &info,
        AnalyzerConfig {
            narrowing: true,
            ..Default::default()
        },
    );
    let exit = ia_solver::analyzer::exit_intervals(
        &program,
        &info,
        AnalyzerConfig {
            narrowing: true,
            ..Default::default()
        },
    );
    // The sole loop performed both ascending and descending iterations.
    let (_, stats) = &narrowed.fixpoints[0];
    assert!(stats.iterations >= 2, "ascending iterations: {}", stats.iterations);
    assert!(stats.narrowing_iterations >= 1);
    // Post-loop x must be within [0, 5], not [0, i64::MAX].
    let x = exit["x"];
    let (lo, hi) = x.bounds().unwrap();
    assert_eq!(lo, 0);
    assert!(hi <= 5, "narrowed upper bound {hi} should be <= 5");
}

#[test]
fn widen_without_narrow_remains_a_sound_overapproximation() {
    let src = "input n [0: 5]; {\nx = 0;\nwhile (x < n) { x = x + 1; }\n}\n";
    let (program, info) = compile(src);
    let exit = ia_solver::analyzer::exit_intervals(
        &program,
        &info,
        AnalyzerConfig {
            narrowing: false,
            ..Default::default()
        },
    );
    // Without narrowing the widened bound stays at the sentinel MAX: less
    // precise, but still an over-approximation containing every concrete x.
    let (lo, hi) = exit["x"].bounds().unwrap();
    assert_eq!(lo, 0);
    // Without descending narrowing the loop invariant is left at the widened
    // bound; the exit guard alone cannot recover the tight constant. The
    // result is therefore strictly looser than the narrowed analysis, while
    // still containing every concrete x in [0, 5] (sound over-approximation).
    assert!(hi > 5, "without narrowing the bound should stay imprecise, got {hi}");
    assert_eq!(
        ia_solver::analyzer::exit_intervals(
            &program,
            &info,
            AnalyzerConfig {
                narrowing: true,
                ..Default::default()
            }
        )["x"]
        .bounds()
        .unwrap()
        .1,
        5,
        "narrowing must tighten the same program to 5"
    );
}

#[test]
fn plain_fixpoint_converges_for_tiny_range() {
    let src = "input n [0: 3]; {\nx = 0;\nwhile (x < n) { x = x + 1; }\n}\n";
    let (program, info) = compile(src);
    let report = analyze(
        &program,
        &info,
        AnalyzerConfig {
            plain_fixpoint: true,
            max_widen_iterations: 100,
            ..Default::default()
        },
    );
    assert_eq!(report.fixpoints[0].1.strategy, ia_solver::FixpointStrategy::Plain);
    assert!(!report.fixpoints[0].1.early_cutoff);
    let exit = ia_solver::analyzer::exit_intervals(
        &program,
        &info,
        AnalyzerConfig {
            plain_fixpoint: true,
            max_widen_iterations: 100,
            ..Default::default()
        },
    );
    assert_eq!(exit["x"].bounds(), Some((0, 3)));
}

#[test]
fn branch_narrows_index_to_safe() {
    let src = fixture("02_branch_refine.ial");
    let (program, info) = compile(src);
    let report = analyze(&program, &info, AnalyzerConfig::default());
    // Guarded read (line 9) safe; unguarded read (line 12) possible OOB.
    assert_eq!(
        find(&report, 9, ia_solver::CheckKind::IndexBounds).verdict,
        CheckVerdict::Safe
    );
    assert_eq!(
        find(&report, 12, ia_solver::CheckKind::IndexBounds).verdict,
        CheckVerdict::PossibleFailure
    );
}

#[test]
fn contradictory_branch_is_unreachable() {
    let src = fixture("04_unreachable.ial");
    let (program, info) = compile(src);
    let report = analyze(&program, &info, AnalyzerConfig::default());
    assert_eq!(
        find(&report, 9, ia_solver::CheckKind::Overflow).verdict,
        CheckVerdict::Unreachable
    );
}

#[test]
fn guaranteed_vs_possible_overflow_distinguished() {
    let src = fixture("03_overflow_paths.ial");
    let (program, info) = compile(src);
    let report = analyze(&program, &info, AnalyzerConfig::default());
    // Line 10 possible; line 20 (MIN / -1) guaranteed.
    assert_eq!(
        find(&report, 10, ia_solver::CheckKind::Overflow).verdict,
        CheckVerdict::PossibleFailure
    );
    assert_eq!(
        find(&report, 20, ia_solver::CheckKind::Overflow).verdict,
        CheckVerdict::GuaranteedFailure
    );
    // Safe divisor on line 17.
    assert_eq!(
        find(&report, 17, ia_solver::CheckKind::DivByZero).verdict,
        CheckVerdict::Safe
    );
}

#[test]
fn interval_arithmetic_is_bounded_not_math_integer() {
    use ia_intervals::binary;
    use ia_lang::ast::BinOp;
    // [MAX-1, MAX] + 1: possible overflow, result TOP, flag set — no panic,
    // no silent wrap to a negative value.
    let (r, f) = binary(
        BinOp::Add,
        Interval::Range {
            lo: i64::MAX - 1,
            hi: i64::MAX,
        },
        Interval::point(1),
    );
    assert!(f.overflow_possible);
    assert_eq!(r, Interval::TOP);
    // Exact arithmetic when no overflow.
    let (r, f) = binary(BinOp::Add, Interval::point(2), Interval::point(3));
    assert!(!f.any());
    assert_eq!(r, Interval::point(5));
}

#[test]
fn widening_jumps_to_bounded_sentinels_not_infinity() {
    // The domain has no ±∞ beyond i64; widened endpoints are exactly MIN/MAX.
    let prev = Interval::point(0);
    let cand = Interval::Range { lo: 0, hi: 1 };
    assert_eq!(
        cand.widen(prev),
        Interval::Range {
            lo: 0,
            hi: i64::MAX
        }
    );
    let prev = Interval::point(0);
    let cand = Interval::Range { lo: -1, hi: 0 };
    assert_eq!(
        cand.widen(prev),
        Interval::Range {
            lo: i64::MIN,
            hi: 0
        }
    );
}

#[test]
fn reports_carry_source_spans_and_trace() {
    let src = fixture("05_mod_assert.ial");
    let (program, info) = compile(src);
    let report = analyze(&program, &info, AnalyzerConfig::default());
    for c in &report.checks {
        assert!(c.span.start.line >= 1);
        assert!(c.span.start.column >= 1);
        assert!(c.span.end.offset >= c.span.start.offset);
    }
    assert!(report
        .trace
        .iter()
        .any(|t| t.kind == "assign" || t.kind == "assert"));
}
