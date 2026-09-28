//! Interval domain with infinite bounds.
//!
//! All concrete values are bounded i64 (see [`I64_MIN`]/[`I64_MAX`]), but the
//! abstract semantics needs intermediate values *outside* that range so that
//! arithmetic overflow is detected precisely: transfer functions compute the
//! mathematical result interval first, classify how it intersects the i64
//! range, and only then clamp (see [`Interval::clamp_to_i64`]). The unbounded
//! intermediate numbers are always immediately classified against the i64
//! boundary and never mixed into the state as if they were concrete values.

use crate::lang::CmpOp;
use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::fmt;

pub const I64_MIN: i128 = i64::MIN as i128; // -9223372036854775808
pub const I64_MAX: i128 = i64::MAX as i128; //  9223372036854775807

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Bound {
    NegInf,
    Fin(i128),
    PosInf,
}

// serde_json has no i128 support, so finite bounds round-trip as strings
// ("-9223372036854775808") while infinite bounds keep their unit-tag form.
impl Serialize for Bound {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self {
            Bound::NegInf => s.serialize_unit_variant("Bound", 0, "NegInf"),
            Bound::PosInf => s.serialize_unit_variant("Bound", 2, "PosInf"),
            Bound::Fin(v) => s.serialize_newtype_variant("Bound", 1, "Fin", &v.to_string()),
        }
    }
}

impl<'de> Deserialize<'de> for Bound {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = serde_json::Value::deserialize(d)?;
        match v {
            serde_json::Value::String(s) => match s.as_str() {
                "NegInf" => Ok(Bound::NegInf),
                "PosInf" => Ok(Bound::PosInf),
                other => other
                    .parse::<i128>()
                    .map(Bound::Fin)
                    .map_err(|_| serde::de::Error::custom(format!("invalid bound `{other}`"))),
            },
            serde_json::Value::Object(map) => {
                if let Some(f) = map.get("Fin") {
                    let s = f
                        .as_str()
                        .ok_or_else(|| serde::de::Error::custom("Fin must be a string"))?;
                    s.parse::<i128>()
                        .map(Bound::Fin)
                        .map_err(serde::de::Error::custom)
                } else if map.contains_key("NegInf") {
                    Ok(Bound::NegInf)
                } else if map.contains_key("PosInf") {
                    Ok(Bound::PosInf)
                } else {
                    Err(serde::de::Error::custom("unknown bound representation"))
                }
            }
            other => Err(serde::de::Error::custom(format!(
                "invalid bound JSON: {other}"
            ))),
        }
    }
}

impl Bound {
    fn rank(self) -> i8 {
        match self {
            Bound::NegInf => -1,
            Bound::Fin(_) => 0,
            Bound::PosInf => 1,
        }
    }

    pub fn finite(self) -> Option<i128> {
        match self {
            Bound::Fin(v) => Some(v),
            _ => None,
        }
    }

    fn sign(self) -> i8 {
        match self {
            Bound::NegInf => -1,
            Bound::Fin(v) => v.signum() as i8,
            Bound::PosInf => 1,
        }
    }

    fn addb(self, other: Bound) -> Bound {
        match (self, other) {
            (Bound::Fin(a), Bound::Fin(b)) => match a.checked_add(b) {
                Some(v) => Bound::Fin(v),
                None => {
                    if a > 0 {
                        Bound::PosInf
                    } else {
                        Bound::NegInf
                    }
                }
            },
            (Bound::PosInf, Bound::PosInf)
            | (Bound::PosInf, Bound::Fin(_))
            | (Bound::Fin(_), Bound::PosInf) => Bound::PosInf,
            (Bound::NegInf, Bound::NegInf)
            | (Bound::NegInf, Bound::Fin(_))
            | (Bound::Fin(_), Bound::NegInf) => Bound::NegInf,
            // NegInf + PosInf cannot occur with well-formed intervals (lo <= hi).
            (Bound::NegInf, Bound::PosInf) | (Bound::PosInf, Bound::NegInf) => {
                unreachable!("neg_inf + pos_inf on a well-formed interval")
            }
        }
    }

    fn negb(self) -> Bound {
        match self {
            Bound::NegInf => Bound::PosInf,
            Bound::Fin(v) => Bound::Fin(-v),
            Bound::PosInf => Bound::NegInf,
        }
    }

    fn subb(self, other: Bound) -> Bound {
        self.addb(other.negb())
    }

    fn mulb(self, other: Bound) -> Bound {
        match (self, other) {
            (Bound::Fin(a), Bound::Fin(b)) => match a.checked_mul(b) {
                Some(v) => Bound::Fin(v),
                None => {
                    if a.signum() == b.signum() {
                        Bound::PosInf
                    } else {
                        Bound::NegInf
                    }
                }
            },
            // Extended-interval convention 0 * (+/- inf) = 0; this stays a
            // sound over-approximation for intervals straddling zero.
            (Bound::Fin(0), _) | (_, Bound::Fin(0)) => Bound::Fin(0),
            _ => {
                if self.sign() * other.sign() > 0 {
                    Bound::PosInf
                } else {
                    Bound::NegInf
                }
            }
        }
    }
}

impl PartialOrd for Bound {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Bound {
    fn cmp(&self, other: &Self) -> Ordering {
        match (self, other) {
            (Bound::Fin(a), Bound::Fin(b)) => a.cmp(b),
            _ => self.rank().cmp(&other.rank()),
        }
    }
}

impl fmt::Display for Bound {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Bound::NegInf => write!(f, "-inf"),
            Bound::Fin(v) => write!(f, "{v}"),
            Bound::PosInf => write!(f, "inf"),
        }
    }
}

/// A closed interval, or the empty interval (`Bottom`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Interval {
    Bottom,
    R { lo: Bound, hi: Bound },
}

impl Interval {
    pub fn constant(v: i128) -> Interval {
        Interval::R {
            lo: Bound::Fin(v),
            hi: Bound::Fin(v),
        }
    }

    pub fn from(lo: Bound, hi: Bound) -> Interval {
        assert!(lo <= hi, "malformed interval: {lo} > {hi}");
        Interval::R { lo, hi }
    }

    pub fn finite(lo: i128, hi: i128) -> Interval {
        Interval::from(Bound::Fin(lo), Bound::Fin(hi))
    }

    pub fn top() -> Interval {
        Interval::R {
            lo: Bound::NegInf,
            hi: Bound::PosInf,
        }
    }

    pub fn i64_range() -> Interval {
        Interval::finite(I64_MIN, I64_MAX)
    }

    pub fn is_bottom(&self) -> bool {
        matches!(self, Interval::Bottom)
    }

    /// Well-formedness used by the evidence checker.
    pub fn is_well_formed(&self) -> bool {
        match self {
            Interval::Bottom => true,
            Interval::R { lo, hi } => lo <= hi,
        }
    }

    pub fn subset_of(&self, other: &Interval) -> bool {
        match (self, other) {
            (Interval::Bottom, _) => true,
            (_, Interval::Bottom) => false,
            (Interval::R { lo: a1, hi: a2 }, Interval::R { lo: b1, hi: b2 }) => {
                b1 <= a1 && a2 <= b2
            }
        }
    }

    pub fn contains_i64(&self, v: i64) -> bool {
        match self {
            Interval::Bottom => false,
            Interval::R { lo, hi } => *lo <= Bound::Fin(v as i128) && Bound::Fin(v as i128) <= *hi,
        }
    }

    pub fn join(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::Bottom, x) | (x, Interval::Bottom) => x,
            (Interval::R { lo: l1, hi: h1 }, Interval::R { lo: l2, hi: h2 }) => {
                Interval::from(l1.min(l2), h1.max(h2))
            }
        }
    }

    pub fn meet(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::Bottom, _) | (_, Interval::Bottom) => Interval::Bottom,
            (Interval::R { lo: l1, hi: h1 }, Interval::R { lo: l2, hi: h2 }) => {
                let lo = l1.max(l2);
                let hi = h1.min(h2);
                if lo <= hi {
                    Interval::from(lo, hi)
                } else {
                    Interval::Bottom
                }
            }
        }
    }

    /// Standard interval widening: a bound that grew jumps to +/- infinity.
    pub fn widen(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::Bottom, x) | (x, Interval::Bottom) => x,
            (Interval::R { lo: l1, hi: h1 }, Interval::R { lo: l2, hi: h2 }) => {
                let lo = if l2 < l1 { Bound::NegInf } else { l1 };
                let hi = if h2 > h1 { Bound::PosInf } else { h1 };
                Interval::from(lo, hi)
            }
        }
    }

    /// Standard interval narrowing: pull infinite bounds back toward `other`'s
    /// finite bounds; never widens.
    pub fn narrow(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::Bottom, _) | (_, Interval::Bottom) => Interval::Bottom,
            (Interval::R { lo: l1, hi: h1 }, Interval::R { lo: l2, hi: h2 }) => {
                let lo = if matches!(l1, Bound::NegInf) { l2 } else { l1 };
                let hi = if matches!(h1, Bound::PosInf) { h2 } else { h1 };
                if lo <= hi {
                    Interval::from(lo, hi)
                } else {
                    Interval::Bottom
                }
            }
        }
    }

    /// Intersect the mathematical result interval with the i64 value domain
    /// and classify how much of the result lies outside. The returned interval
    /// over-approximates exactly the executions that do NOT overflow.
    pub fn clamp_to_i64(self) -> (Interval, OverflowClass) {
        match self {
            Interval::Bottom => (Interval::Bottom, OverflowClass::Inside),
            r => {
                let inside = r.meet(Interval::i64_range());
                if inside == r {
                    (r, OverflowClass::Inside)
                } else if inside.is_bottom() {
                    (Interval::Bottom, OverflowClass::EntirelyOutside)
                } else {
                    (inside, OverflowClass::PartiallyOutside)
                }
            }
        }
    }

    /// Refine the *both* sides of a comparison. Returns the refined interval
    /// for each side (Bottom means the comparison is infeasible).
    pub fn refine_cmp(op: CmpOp, lhs: Interval, rhs: Interval) -> (Interval, Interval) {
        use Bound::*;
        match (lhs, rhs) {
            (Interval::Bottom, _) | (_, Interval::Bottom) => (Interval::Bottom, Interval::Bottom),
            (Interval::R { lo: ll, hi: lh }, Interval::R { lo: rl, hi: rh }) => {
                let (l, r) = match op {
                    CmpOp::Eq => {
                        let m_lo = ll.max(rl);
                        let m_hi = lh.min(rh);
                        let m = if m_lo <= m_hi {
                            Interval::from(m_lo, m_hi)
                        } else {
                            Interval::Bottom
                        };
                        return (m, m);
                    }
                    CmpOp::Ne => {
                        // Only meaningful against a singleton.
                        let lhs_prime = remove_point(ll, lh, rh, rl);
                        let rhs_prime = remove_point(rl, rh, lh, ll);
                        return (lhs_prime, rhs_prime);
                    }
                    CmpOp::Lt => (
                        Interval::from(NegInf, rh.subb(Fin(1))),
                        Interval::from(ll.addb(Fin(1)), PosInf),
                    ),
                    CmpOp::Le => (Interval::from(NegInf, rh), Interval::from(ll, PosInf)),
                    CmpOp::Gt => (
                        Interval::from(rl.addb(Fin(1)), PosInf),
                        Interval::from(NegInf, lh.subb(Fin(1))),
                    ),
                    CmpOp::Ge => (Interval::from(rl, PosInf), Interval::from(NegInf, lh)),
                };
                (lhs.meet(l), rhs.meet(r))
            }
        }
    }
}

// Standard arithmetic operators: bottom propagates (any arithmetic on an
// unreachable state is itself unreachable).
impl std::ops::Add for Interval {
    type Output = Interval;
    fn add(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::R { lo: l1, hi: h1 }, Interval::R { lo: l2, hi: h2 }) => {
                Interval::from(l1.addb(l2), h1.addb(h2))
            }
            _ => Interval::Bottom,
        }
    }
}

impl std::ops::Sub for Interval {
    type Output = Interval;
    fn sub(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::R { lo: l1, hi: h1 }, Interval::R { lo: l2, hi: h2 }) => {
                Interval::from(l1.subb(h2), h1.subb(l2))
            }
            _ => Interval::Bottom,
        }
    }
}

impl std::ops::Mul for Interval {
    type Output = Interval;
    fn mul(self, other: Interval) -> Interval {
        match (self, other) {
            (Interval::R { lo: l1, hi: h1 }, Interval::R { lo: l2, hi: h2 }) => {
                let candidates = [l1.mulb(l2), l1.mulb(h2), h1.mulb(l2), h1.mulb(h2)];
                let lo = candidates.iter().copied().min().unwrap();
                let hi = candidates.iter().copied().max().unwrap();
                Interval::from(lo, hi)
            }
            _ => Interval::Bottom,
        }
    }
}

impl std::ops::Neg for Interval {
    type Output = Interval;
    fn neg(self) -> Interval {
        match self {
            Interval::R { lo, hi } => Interval::from(hi.negb(), lo.negb()),
            Interval::Bottom => Interval::Bottom,
        }
    }
}

/// Remove a singleton point `p` (p.lo == p.hi) from interval [lo, hi]; only
/// refines when the point is exactly an endpoint.
fn remove_point(lo: Bound, hi: Bound, p_hi: Bound, p_lo: Bound) -> Interval {
    if p_hi == p_lo {
        let p = p_hi;
        if lo == p && hi == p {
            Interval::Bottom
        } else if lo == p {
            Interval::from(p.addb(Bound::Fin(1)), hi)
        } else if hi == p {
            Interval::from(lo, p.subb(Bound::Fin(1)))
        } else {
            Interval::from(lo, hi)
        }
    } else {
        Interval::from(lo, hi)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum OverflowClass {
    /// The whole mathematical result fits in i64.
    Inside,
    /// Part lies inside i64, part outside: some executions overflow.
    PartiallyOutside,
    /// No part fits in i64: every execution overflows.
    EntirelyOutside,
}

impl fmt::Display for Interval {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Interval::Bottom => write!(f, "empty"),
            Interval::R { lo, hi } => write!(f, "[{lo}, {hi}]"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use Bound::{Fin, NegInf, PosInf};

    #[test]
    fn ordering_and_arith_finite() {
        assert_eq!(
            Interval::finite(1, 2) + Interval::finite(3, 4),
            Interval::finite(4, 6)
        );
        assert_eq!(
            Interval::finite(1, 3) - Interval::finite(-2, 5),
            Interval::finite(-4, 5)
        );
        assert_eq!(-Interval::finite(-3, -1), Interval::finite(1, 3));
        assert_eq!(
            Interval::finite(-2, 3) * Interval::finite(-4, 5),
            Interval::finite(-12, 15)
        );
        assert_eq!(
            Interval::constant(0) * Interval::top(),
            Interval::constant(0)
        );
    }

    #[test]
    fn arith_with_infinities() {
        assert_eq!(
            Interval::from(Fin(0), PosInf) + Interval::constant(1),
            Interval::from(Fin(1), PosInf)
        );
        assert_eq!(
            Interval::from(NegInf, Fin(0)) * Interval::from(Fin(2), PosInf),
            Interval::from(NegInf, Fin(0))
        );
        assert_eq!(
            Interval::from(Fin(-2), Fin(3)) * Interval::from(Fin(5), PosInf),
            Interval::top()
        );
    }

    #[test]
    fn join_meet_lattice() {
        let a = Interval::finite(1, 4);
        let b = Interval::finite(3, 8);
        assert_eq!(a.join(b), Interval::finite(1, 8));
        assert_eq!(a.meet(b), Interval::finite(3, 4));
        assert!(Interval::finite(3, 4).subset_of(&a));
        assert!(!a.subset_of(&b));
    }

    #[test]
    fn widen_and_narrow() {
        let w = Interval::finite(0, 1).widen(Interval::finite(0, 2));
        assert_eq!(w, Interval::from(Fin(0), PosInf));
        // narrowing re-pulls the infinite bound but never expands
        assert_eq!(w.narrow(Interval::finite(0, 10)), Interval::finite(0, 10));
        assert_eq!(
            Interval::finite(0, 10).narrow(Interval::finite(0, 20)),
            Interval::finite(0, 10)
        );
    }

    #[test]
    fn cmp_refinement() {
        let (l, r) =
            Interval::refine_cmp(CmpOp::Lt, Interval::finite(0, 10), Interval::finite(7, 7));
        assert_eq!(l, Interval::finite(0, 6));
        assert_eq!(r, Interval::finite(7, 7));
        let (l, r) =
            Interval::refine_cmp(CmpOp::Eq, Interval::finite(0, 5), Interval::finite(5, 10));
        assert_eq!((l, r), (Interval::constant(5), Interval::constant(5)));
        let (l, _) = Interval::refine_cmp(CmpOp::Ne, Interval::finite(3, 3), Interval::constant(3));
        assert_eq!(l, Interval::Bottom);
        let (l, _) = Interval::refine_cmp(CmpOp::Ne, Interval::finite(0, 3), Interval::constant(3));
        assert_eq!(l, Interval::finite(0, 2));
    }

    #[test]
    fn overflow_classification() {
        let (r, c) = Interval::finite(1, 2).clamp_to_i64();
        assert_eq!(c, OverflowClass::Inside);
        assert_eq!(r, Interval::finite(1, 2));
        // i64::MAX+1 is entirely outside on multiplication ranges
        let (_, c) = Interval::finite(I64_MAX + 1, I64_MAX + 100).clamp_to_i64();
        assert_eq!(c, OverflowClass::EntirelyOutside);
        let (_, c) = Interval::finite(I64_MAX - 5, I64_MAX + 5).clamp_to_i64();
        assert_eq!(c, OverflowClass::PartiallyOutside);
    }
}
