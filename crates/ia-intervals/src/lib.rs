//! Bounded interval abstract domain over `i64`.
//!
//! Semantics (see docs/SEMANTICS.md §2):
//! * Every abstracted value is a closed interval `[lo, hi]` of **bounded**
//!   signed 64-bit integers. There is no `-∞/+∞` represented as a big integer:
//!   the sentinels [`i64::MIN`]/[`i64::MAX`] stand for "full domain", which is
//!   the most precise unbounded-looking approximation expressible in i64.
//! * [`Interval::BOTTOM`] denotes the empty set: a state mapped to bottom is
//!   unreachable on the current path.
//! * Arithmetic mirrors the concrete executor exactly (`wrapping = false`,
//!   i.e. Rust checked semantics). The returned [`ArithFlag`] separately
//!   records whether division-by-zero and/or overflow is *possible* somewhere
//!   in the interval. When a failure is possible the value is pushed to
//!   [`Interval::TOP`] for that argument combination: this is an
//!   over-approximation, never a claim that every input fails.
use serde::{Deserialize, Serialize};

/// Result quality of an abstract arithmetic operation.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArithFlag {
    /// Some concrete choice of operands within the argument intervals divides
    /// (or takes modulo) by zero.
    pub div_by_zero_possible: bool,
    /// Some concrete choice of operands provokes checked i64 overflow
    /// (including `i64::MIN / -1` and `-i64::MIN`).
    pub overflow_possible: bool,
}

impl ArithFlag {
    pub fn none() -> Self {
        Self::default()
    }
    pub fn merge(self, other: Self) -> Self {
        Self {
            div_by_zero_possible: self.div_by_zero_possible || other.div_by_zero_possible,
            overflow_possible: self.overflow_possible || other.overflow_possible,
        }
    }
    pub fn any(self) -> bool {
        self.div_by_zero_possible || self.overflow_possible
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Interval {
    /// Empty set; unreachable.
    Bottom,
    /// Closed non-empty interval of i64.
    Range { lo: i64, hi: i64 },
}

impl Interval {
    pub const BOTTOM: Interval = Interval::Bottom;
    pub const TOP: Interval = Interval::Range {
        lo: i64::MIN,
        hi: i64::MAX,
    };
    pub const ZERO: Interval = Interval::Range { lo: 0, hi: 0 };
    pub const ONE: Interval = Interval::Range { lo: 1, hi: 1 };
    pub const BOOL: Interval = Interval::Range { lo: 0, hi: 1 };

    pub fn point(v: i64) -> Self {
        Interval::Range { lo: v, hi: v }
    }

    pub fn new(lo: i64, hi: i64) -> Option<Self> {
        if lo <= hi {
            Some(Interval::Range { lo, hi })
        } else {
            None
        }
    }

    pub fn is_bottom(self) -> bool {
        matches!(self, Interval::Bottom)
    }

    pub fn is_empty(self) -> bool {
        self.is_bottom()
    }

    pub fn is_point(self) -> Option<i64> {
        match self {
            Interval::Range { lo, hi } if lo == hi => Some(lo),
            _ => None,
        }
    }

    /// Lower and upper bounds, or `None` for bottom.
    pub fn bounds(self) -> Option<(i64, i64)> {
        match self {
            Interval::Range { lo, hi } => Some((lo, hi)),
            Interval::Bottom => None,
        }
    }

    pub fn contains(self, v: i64) -> bool {
        match self {
            Interval::Bottom => false,
            Interval::Range { lo, hi } => lo <= v && v <= hi,
        }
    }

    /// Smallest interval containing both. Bottom is the identity.
    pub fn join(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::Bottom, x) | (x, Interval::Bottom) => x,
            (
                Interval::Range { lo: a, hi: b },
                Interval::Range { lo: c, hi: d },
            ) => Interval::Range {
                lo: a.min(c),
                hi: b.max(d),
            },
        }
    }

    /// Set intersection.
    pub fn meet(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::Bottom, _) | (_, Interval::Bottom) => Interval::Bottom,
            (
                Interval::Range { lo: a, hi: b },
                Interval::Range { lo: c, hi: d },
            ) => {
                let lo = a.max(c);
                let hi = b.min(d);
                Interval::new(lo, hi).unwrap_or(Interval::Bottom)
            }
        }
    }

    /// Inclusion test used by the fixpoint loop.
    pub fn subset_of(self, other: Interval) -> bool {
        match (self, other) {
            (Interval::Bottom, _) => true,
            (_, Interval::Bottom) => false,
            (
                Interval::Range { lo: a, hi: b },
                Interval::Range { lo: c, hi: d },
            ) => c <= a && b <= d,
        }
    }

    /// Classic jump-to-bound widening: any unstable endpoint jumps straight to
    /// the corresponding bounded-domain sentinel. Monotone, and reaches a
    /// post-fixpoint in finitely many steps because only i64::MIN/i64::MAX are
    /// ever introduced beyond the operands' endpoints.
    pub fn widen(self, previous: Interval) -> Interval {
        match (self, previous) {
            (x, Interval::Bottom) => x,
            (Interval::Bottom, p) => p,
            (
                Interval::Range { lo: a, hi: b },
                Interval::Range { lo: c, hi: d },
            ) => Interval::Range {
                lo: if a < c { i64::MIN } else { c },
                hi: if b > d { i64::MAX } else { d },
            },
        }
    }

    /// Bounded narrowing: intersect with the candidate, which monotonically
    /// descends from a widened post-fixpoint. One application is the standard
    /// `∇ then Δ`; callers apply it for a bounded number of iterations.
    pub fn narrow(self, candidate: Interval) -> Interval {
        self.meet(candidate)
    }

    fn neg(self) -> (Interval, ArithFlag) {
        match self {
            Interval::Bottom => (Interval::Bottom, ArithFlag::none()),
            Interval::Range { lo, hi } => {
                let mut flag = ArithFlag::none();
                // `-i64::MIN` overflows.
                if lo == i64::MIN {
                    flag.overflow_possible = true;
                }
                let (nlo, nhi) = match (lo.checked_neg(), hi.checked_neg()) {
                    (Some(a), Some(b)) => (a.min(b), a.max(b)),
                    // lo == MIN: exact negation impossible on that endpoint;
                    // approximate the well-defined portion [-hi, i64::MAX],
                    // joining with TOP for the failing choice.
                    _ => (-hi.max(i64::MIN + 1), i64::MAX),
                };
                let result = if flag.overflow_possible {
                    Interval::TOP
                } else {
                    Interval::Range { lo: nlo, hi: nhi }
                };
                (result, flag)
            }
        }
    }

    fn add(self, rhs: Interval) -> (Interval, ArithFlag) {
        match (self.bounds(), rhs.bounds()) {
            (None, _) | (_, None) => (Interval::Bottom, ArithFlag::none()),
            (Some((a, b)), Some((c, d))) => {
                let lo = (a as i128) + (c as i128);
                let hi = (b as i128) + (d as i128);
                let overflow = lo < i64::MIN as i128 || hi > i64::MAX as i128;
                if overflow {
                    (Interval::TOP, ArithFlag { overflow_possible: true, ..ArithFlag::none() })
                } else {
                    (Interval::Range { lo: lo as i64, hi: hi as i64 }, ArithFlag::none())
                }
            }
        }
    }

    fn sub(self, rhs: Interval) -> (Interval, ArithFlag) {
        match (self.bounds(), rhs.bounds()) {
            (None, _) | (_, None) => (Interval::Bottom, ArithFlag::none()),
            (Some((a, b)), Some((c, d))) => {
                let lo = (a as i128) - (d as i128);
                let hi = (b as i128) - (c as i128);
                let overflow = lo < i64::MIN as i128 || hi > i64::MAX as i128;
                if overflow {
                    (Interval::TOP, ArithFlag { overflow_possible: true, ..ArithFlag::none() })
                } else {
                    (Interval::Range { lo: lo as i64, hi: hi as i64 }, ArithFlag::none())
                }
            }
        }
    }

    /// Exact image of multiplication over i128 endpoints; overflow is judged
    /// against the i64 boundary before casting. Multiplication endpoints are
    /// attained at interval corners (bilinear monotonicity per quadrant).
    fn mul(self, rhs: Interval) -> (Interval, ArithFlag) {
        match (self.bounds(), rhs.bounds()) {
            (None, _) | (_, None) => (Interval::Bottom, ArithFlag::none()),
            (Some((a, b)), Some((c, d))) => {
                let xs = [a as i128, b as i128];
                let ys = [c as i128, d as i128];
                let mut lo = i128::MAX;
                let mut hi = i128::MIN;
                for x in xs {
                    for y in ys {
                        let p = x * y;
                        lo = lo.min(p);
                        hi = hi.max(p);
                    }
                }
                let overflow = lo < i64::MIN as i128 || hi > i64::MAX as i128;
                if overflow {
                    (Interval::TOP, ArithFlag { overflow_possible: true, ..ArithFlag::none() })
                } else {
                    (Interval::Range { lo: lo as i64, hi: hi as i64 }, ArithFlag::none())
                }
            }
        }
    }

    /// Division/modulo helper. Splits the divisor interval into a strictly
    /// positive and a strictly negative partition (excluding zero), evaluates
    /// each with the exact truncation-toward-zero image, and joins. A divisor
    /// interval containing zero separately sets `div_by_zero_possible`.
    fn divmod(self, rhs: Interval, is_div: bool) -> (Interval, ArithFlag) {
        let ((a, b), (c, d)) = match (self.bounds(), rhs.bounds()) {
            (Some(x), Some(y)) => (x, y),
            _ => return (Interval::Bottom, ArithFlag::none()),
        };
        let mut flag = ArithFlag::none();
        if c <= 0 && 0 <= d {
            flag.div_by_zero_possible = true;
        }
        let mut acc = Interval::Bottom;
        if d >= 1 {
            // Positive divisors [max(c,1), d].
            let pc = c.max(1);
            if pc <= d {
                acc = acc.join(if is_div {
                    trunc_div_image(a, b, pc, d)
                } else {
                    trunc_mod_image(a, b, pc, d)
                });
            }
        }
        if c <= -1 {
            // Negative divisors [c, min(d,-1)]: reduce to positive-denominator
            // formulas by sign mirroring (truncation is symmetric).
            let nd = d.min(-1);
            if c <= nd {
                let pos_c = (-nd).max(1);
                let pos_d = -c;
                let part = if is_div {
                    trunc_div_image(a, b, pos_c, pos_d)
                } else {
                    trunc_mod_image(a, b, pos_c, pos_d)
                };
                acc = acc.join(part);
            }
        }
        // MIN / -1 overflow is possible iff dividend reaches MIN and the
        // divisor interval contains -1.
        if is_div && a == i64::MIN && c <= -1 && -1 <= d {
            flag.overflow_possible = true;
        }
        let result = if flag.any() {
            // Failing choices map to TOP and are reported separately; this is
            // an over-approximation, never confused with the well-defined
            // image.
            acc.join(Interval::TOP)
        } else {
            acc
        };
        (result, flag)
    }
}

/// Exact image of truncated integer division `x / y` for
/// `x ∈ [a,b]`, `y ∈ [c,d]` with `1 <= c <= d`. Division is monotone in `x`
/// for each fixed sign of `x` and monotone-decreasing in `|y|`, so the extrema
/// over the rectangle are taken at the four corners.
fn trunc_div_image(a: i64, b: i64, c: i64, d: i64) -> Interval {
    debug_assert!(c >= 1);
    let xs = [a as i128, b as i128];
    let ys = [c as i128, d as i128];
    let mut lo = i128::MAX;
    let mut hi = i128::MIN;
    for x in xs {
        for y in ys {
            let q = x / y;
            lo = lo.min(q);
            hi = hi.max(q);
        }
    }
    Interval::Range {
        lo: lo.max(i64::MIN as i128).min(i64::MAX as i128) as i64,
        hi: hi.max(i64::MIN as i128).min(i64::MAX as i128) as i64,
    }
}

/// Exact image of truncated remainder `x % y` for `x ∈ [a,b]`,
/// `y ∈ [c,d]` with `1 <= c <= d`.
///
/// The corner formulas used for division do NOT apply here: the remainder
/// can reach `±(y-1)` at *non-corner* dividends. Using corners is unsound
/// (e.g. `[-7,7] % 3` covers `[-2,2]`, while the corners give `[-1,1]`).
///
/// Derivation (Rust truncation toward zero, `|x % y| < y`,
/// `sign(x % y) == sign(x)`):
/// * nonnegative x reach every remainder `0..=min(b, d-1)`;
/// * negative x reach every remainder `-min(-a, d-1)..=0`.
fn trunc_mod_image(a: i64, b: i64, c: i64, d: i64) -> Interval {
    debug_assert!(c >= 1);
    let _ = c;
    // Smallest (most negative) remainder, attained on the negative-x side:
    // -min(|a|, d-1). `a` is strictly negative here, so `wrapping_abs` is
    // safe to re-negate (|a| <= i64::MAX because a < 0); `d-1 >= 0`.
    let lo = if a < 0 {
        let abs_a = a.wrapping_abs();
        -(abs_a.min(d - 1))
    } else {
        0
    };
    // Largest nonnegative remainder, attained on the positive-x side:
    // min(b, d-1).
    let hi = if b > 0 { b.min(d - 1) } else { 0 };
    Interval::Range { lo, hi }
}

/// Abstract unary operation entry point.
pub fn unary(op: ia_lang::ast::UnOp, x: Interval) -> (Interval, ArithFlag) {
    use ia_lang::ast::UnOp;
    match op {
        UnOp::Neg => x.neg(),
        UnOp::Not => match x {
            Interval::Bottom => (Interval::Bottom, ArithFlag::none()),
            Interval::Range { lo, hi } => {
                let t = lo <= 0 && 0 <= hi;
                let f = lo != 0 || hi != 0;
                match (t, f) {
                    (true, true) => (Interval::BOOL, ArithFlag::none()),
                    (true, false) => (Interval::ONE, ArithFlag::none()),  // only 0
                    (false, true) => (Interval::ZERO, ArithFlag::none()), // no 0
                    (false, false) => (Interval::Bottom, ArithFlag::none()),
                }
            }
        },
    }
}

/// Abstract binary operation entry point.
pub fn binary(op: ia_lang::ast::BinOp, x: Interval, y: Interval) -> (Interval, ArithFlag) {
    use ia_lang::ast::BinOp;
    if x.is_bottom() || y.is_bottom() {
        return (Interval::Bottom, ArithFlag::none());
    }
    match op {
        BinOp::Add => x.add(y),
        BinOp::Sub => x.sub(y),
        BinOp::Mul => x.mul(y),
        BinOp::Div | BinOp::Mod => x.divmod(y, matches!(op, BinOp::Div)),
        BinOp::Lt | BinOp::Le | BinOp::Gt | BinOp::Ge | BinOp::Eq | BinOp::Ne => {
            (compare(op, x, y), ArithFlag::none())
        }
        BinOp::And | BinOp::Or => (Interval::BOOL, ArithFlag::none()),
    }
}

fn compare(op: ia_lang::ast::BinOp, x: Interval, y: Interval) -> Interval {
    use ia_lang::ast::BinOp;
    let (a, b) = x.bounds().unwrap();
    let (c, d) = y.bounds().unwrap();
    // necessary / sufficient tests on endpoint relations
    let all = match op {
        BinOp::Lt => b < c,
        BinOp::Le => b <= c,
        BinOp::Gt => a > d,
        BinOp::Ge => a >= d,
        BinOp::Eq => a == b && c == d && a == c,
        BinOp::Ne => b < c || a > d,
        _ => unreachable!(),
    };
    let none = match op {
        BinOp::Lt => a >= d,
        BinOp::Le => a > d,
        BinOp::Gt => b <= c,
        BinOp::Ge => b < c,
        BinOp::Eq => b < c || a > d,
        BinOp::Ne => a == b && c == d && a == c,
        _ => unreachable!(),
    };
    match (all, none) {
        (true, _) => Interval::ONE,
        (_, true) => Interval::ZERO,
        _ => Interval::BOOL,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn join_widen_narrow_chain() {
        let i0 = Interval::point(0);
        let i1 = i0.join(Interval::point(1));
        let w = i1.widen(i0);
        assert_eq!(w, Interval::Range { lo: 0, hi: i64::MAX });
        let n = w.narrow(Interval::Range { lo: 0, hi: 5 });
        assert_eq!(n, Interval::Range { lo: 0, hi: 5 });
    }

    #[test]
    fn overflow_flag_on_add() {
        let (r, f) = binary(
            ia_lang::ast::BinOp::Add,
            Interval::Range { lo: i64::MAX - 1, hi: i64::MAX },
            Interval::point(1),
        );
        assert!(f.overflow_possible);
        assert_eq!(r, Interval::TOP);
    }

    #[test]
    fn div_by_zero_partition_is_flagged() {
        let (_, f) = binary(
            ia_lang::ast::BinOp::Div,
            Interval::point(10),
            Interval::Range { lo: -1, hi: 1 },
        );
        assert!(f.div_by_zero_possible);
    }

    #[test]
    fn exact_division_without_zero() {
        let (r, f) = binary(
            ia_lang::ast::BinOp::Div,
            Interval::Range { lo: 7, hi: 9 },
            Interval::Range { lo: 2, hi: 3 },
        );
        assert!(!f.any());
        assert_eq!(r, Interval::Range { lo: 2, hi: 4 });
    }

    #[test]
    fn min_div_minus_one_overflow() {
        let (r, f) = binary(
            ia_lang::ast::BinOp::Div,
            Interval::point(i64::MIN),
            Interval::point(-1),
        );
        assert!(f.overflow_possible);
        assert_eq!(r, Interval::TOP);
    }

    #[test]
    fn neg_min_is_top_with_flag() {
        let (r, f) = unary(ia_lang::ast::UnOp::Neg, Interval::point(i64::MIN));
        assert!(f.overflow_possible);
        assert_eq!(r, Interval::TOP);
    }
}
