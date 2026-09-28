//! Hand-computed unit tests for the interval lattice.
//! These expected values are written from the definitions (not produced by the
//! solver under test).

use interval_analyzer::kernel::{Bound, Interval, I64_MAX, I64_MIN};
use interval_analyzer::lang::CmpOp;
use interval_analyzer::report::classify_vs_range;
use interval_analyzer::report::Verdict;

#[test]
fn pointwise_finite_arithmetic() {
    // [1,3] + [-2,5] = [-1,8]
    assert_eq!(
        Interval::finite(1, 3) + Interval::finite(-2, 5),
        Interval::finite(-1, 8)
    );
    // [1,3] - [-2,5] = [1-5, 3-(-2)] = [-4,5]
    assert_eq!(
        Interval::finite(1, 3) - Interval::finite(-2, 5),
        Interval::finite(-4, 5)
    );
    // [-2,3] * [-4,5] = min(-12,15,-10,-8)... = -12, max 15
    assert_eq!(
        Interval::finite(-2, 3) * Interval::finite(-4, 5),
        Interval::finite(-12, 15)
    );
    // negation flips
    assert_eq!(-Interval::finite(-3, -1), Interval::finite(1, 3));
    // singleton 0 times anything is 0 (extended 0*inf = 0 convention)
    assert_eq!(
        Interval::constant(0) * Interval::top(),
        Interval::constant(0)
    );
}

#[test]
fn infinite_arithmetic() {
    // [0, inf] + 1 = [1, inf]
    assert_eq!(
        Interval::from(Bound::Fin(0), Bound::PosInf) + Interval::constant(1),
        Interval::from(Bound::Fin(1), Bound::PosInf)
    );
    // [-inf,0] * [2,inf] = [-inf,0]
    assert_eq!(
        Interval::from(Bound::NegInf, Bound::Fin(0)) * Interval::from(Bound::Fin(2), Bound::PosInf),
        Interval::from(Bound::NegInf, Bound::Fin(0))
    );
    // [-2,3] * [5,inf] = [-inf, inf]
    assert_eq!(
        Interval::finite(-2, 3) * Interval::from(Bound::Fin(5), Bound::PosInf),
        Interval::top()
    );
    // [1, inf] - [1, inf] = [-inf, inf] (no correlation tracking, as expected)
    assert_eq!(
        Interval::from(Bound::Fin(1), Bound::PosInf) - Interval::from(Bound::Fin(1), Bound::PosInf),
        Interval::top()
    );
}

#[test]
fn widen_jumps_and_narrow_pulls_back() {
    let w = Interval::finite(0, 1).widen(Interval::finite(0, 2));
    assert_eq!(w, Interval::from(Bound::Fin(0), Bound::PosInf));
    let w2 = Interval::finite(0, 5).widen(Interval::finite(-1, 5));
    assert_eq!(w2, Interval::from(Bound::NegInf, Bound::Fin(5)));
    // narrowing only pulls infinite bounds and never expands
    assert_eq!(w.narrow(Interval::finite(0, 10)), Interval::finite(0, 10));
    assert_eq!(
        Interval::finite(0, 10).narrow(Interval::finite(0, 99)),
        Interval::finite(0, 10)
    );
    // bottom identity
    assert_eq!(Interval::Bottom.widen(w), w);
    assert_eq!(w.widen(Interval::Bottom), w);
}

#[test]
fn comparison_refinement_is_symmetric_and_exact() {
    // i < 7 with i in [0,10] -> i in [0,6]
    let (l, _) = Interval::refine_cmp(CmpOp::Lt, Interval::finite(0, 10), Interval::constant(7));
    assert_eq!(l, Interval::finite(0, 6));
    // i >= 5 with i in [0,10] -> [5,10]
    let (l, _) = Interval::refine_cmp(CmpOp::Ge, Interval::finite(0, 10), Interval::constant(5));
    assert_eq!(l, Interval::finite(5, 10));
    // equality on touching singletons
    let (l, r) = Interval::refine_cmp(CmpOp::Eq, Interval::finite(0, 5), Interval::finite(5, 10));
    assert_eq!(l, Interval::constant(5));
    assert_eq!(r, Interval::constant(5));
    // equality on disjoint -> infeasible
    let (l, r) = Interval::refine_cmp(CmpOp::Eq, Interval::finite(0, 4), Interval::finite(5, 10));
    assert_eq!(l, Interval::Bottom);
    assert_eq!(r, Interval::Bottom);
    // != on an endpoint singleton
    let (l, _) = Interval::refine_cmp(CmpOp::Ne, Interval::finite(0, 3), Interval::constant(3));
    assert_eq!(l, Interval::finite(0, 2));
    // != on a singleton interval equal to the point -> infeasible
    let (l, _) = Interval::refine_cmp(CmpOp::Ne, Interval::constant(3), Interval::constant(3));
    assert_eq!(l, Interval::Bottom);
}

#[test]
fn i64_boundary_classification() {
    // strictly inside
    let (r, c) = Interval::finite(1, 2).clamp_to_i64();
    assert_eq!(c, interval_analyzer::kernel::OverflowClass::Inside);
    assert_eq!(r, Interval::finite(1, 2));
    // entire interval above i64::MAX
    let (r, c) = Interval::finite(I64_MAX + 1, I64_MAX + 10).clamp_to_i64();
    assert_eq!(c, interval_analyzer::kernel::OverflowClass::EntirelyOutside);
    assert_eq!(r, Interval::Bottom);
    // entire below i64::MIN
    let (r, c) = Interval::finite(I64_MIN - 10, I64_MIN - 1).clamp_to_i64();
    assert_eq!(c, interval_analyzer::kernel::OverflowClass::EntirelyOutside);
    assert_eq!(r, Interval::Bottom);
    // straddling i64::MAX: continuation is clamped to [.., I64_MAX]
    let (r, c) = Interval::finite(I64_MAX - 5, I64_MAX + 5).clamp_to_i64();
    assert_eq!(
        c,
        interval_analyzer::kernel::OverflowClass::PartiallyOutside
    );
    assert_eq!(r, Interval::finite(I64_MAX - 5, I64_MAX));
}

#[test]
fn verdict_classification_never_calls_partial_overlap_definite() {
    // whole interval outside => definite violation
    assert_eq!(
        classify_vs_range(Interval::constant(I64_MAX + 1), I64_MIN, I64_MAX),
        Verdict::Violated
    );
    // partial overlap => POSSIBLE, never Violated
    assert_eq!(
        classify_vs_range(Interval::finite(I64_MAX - 5, I64_MAX + 5), I64_MIN, I64_MAX),
        Verdict::MaybeViolated
    );
    // contained => safe
    assert_eq!(
        classify_vs_range(Interval::finite(0, 9), 0, 9),
        Verdict::Safe
    );
    // array-style range
    assert_eq!(
        classify_vs_range(Interval::finite(0, 20), 0, 9),
        Verdict::MaybeViolated
    );
}
