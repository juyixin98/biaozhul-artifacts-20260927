//! Branch narrowing (`assume`): refine the abstract state with the truth or
//! falsity of a condition.
//!
//! Soundness rules:
//! * only scalar **affine** constraints `(±1)·x + c ROP k` are used to cut
//!   intervals;
//! * array reads, non-affine expressions and comparisons between two
//!   variables leave the state unchanged (identity is always sound);
//! * when no assignment can satisfy the condition the returned state is
//!   bottom, which is exactly how unreachable branches are detected.
use crate::state::AbsState;
use ia_intervals::Interval;
use ia_lang::ast::*;

/// Affine view `coeff * var + offset` with `coeff ∈ {-1, 1}` and i128 math.
struct Affine {
    var: String,
    coeff: i128,
    offset: i128,
}

fn affine(e: &Expr) -> Option<Affine> {
    fn go(e: &Expr) -> Option<(String, i128, i128)> {
        match &e.kind {
            ExprKind::Int(v) => Some((String::new(), 0, *v as i128)),
            ExprKind::Var(name) => Some((name.clone(), 1, 0)),
            ExprKind::ArrayRead { .. } => None,
            ExprKind::Unary { op: UnOp::Neg, inner } => {
                let (v, c, k) = go(inner)?;
                Some((v, -c, -k))
            }
            ExprKind::Unary { op: UnOp::Not, .. } => None,
            ExprKind::Binary { op, lhs, rhs } => {
                let (v1, c1, k1) = go(lhs)?;
                let (v2, c2, k2) = go(rhs)?;
                match op {
                    BinOp::Add => combine(v1, c1, k1, v2, c2, k2),
                    BinOp::Sub => combine(v1, c1, k1, v2, -c2, -k2),
                    _ => None,
                }
            }
        }
    }
    fn combine(
        v1: String,
        c1: i128,
        k1: i128,
        v2: String,
        c2: i128,
        k2: i128,
    ) -> Option<(String, i128, i128)> {
        let var = match (v1.is_empty(), v2.is_empty()) {
            (true, true) => String::new(),
            (false, true) if c2 == 0 => v1,
            (true, false) if c1 == 0 => v2,
            _ => return None, // two distinct variables: not a single-variable affine
        };
        Some((var, c1 + c2, k1 + k2))
    }
    let (var, coeff, offset) = go(e)?;
    if !(coeff == 1 || coeff == -1 || (var.is_empty() && coeff == 0)) {
        return None;
    }
    Some(Affine { var, coeff, offset })
}

#[derive(Clone, Copy)]
enum Rel {
    Lt,
    Le,
    Gt,
    Ge,
    Eq,
    Ne,
}

impl Rel {
    fn flip(self) -> Rel {
        match self {
            Rel::Lt => Rel::Ge,
            Rel::Le => Rel::Gt,
            Rel::Gt => Rel::Le,
            Rel::Ge => Rel::Lt,
            Rel::Eq => Rel::Ne,
            Rel::Ne => Rel::Eq,
        }
    }
}

fn rel_of(op: BinOp) -> Option<Rel> {
    Some(match op {
        BinOp::Lt => Rel::Lt,
        BinOp::Le => Rel::Le,
        BinOp::Gt => Rel::Gt,
        BinOp::Ge => Rel::Ge,
        BinOp::Eq => Rel::Eq,
        BinOp::Ne => Rel::Ne,
        _ => return None,
    })
}

fn swap_rel(rel: Rel) -> Rel {
    match rel {
        Rel::Lt => Rel::Gt,
        Rel::Le => Rel::Ge,
        Rel::Gt => Rel::Lt,
        Rel::Ge => Rel::Le,
        Rel::Eq => Rel::Eq,
        Rel::Ne => Rel::Ne,
    }
}

/// Refine `state` under `cond` being true (`truth == true`) or false.
pub fn assume(state: &AbsState, cond: &Expr, truth: bool) -> AbsState {
    if state.is_bottom() {
        return AbsState::bottom();
    }
    match &cond.kind {
        ExprKind::Int(v) => {
            let holds = (*v != 0) == truth;
            if holds { state.clone() } else { AbsState::bottom() }
        }
        ExprKind::Unary { op: UnOp::Not, inner } => assume(state, inner, !truth),
        ExprKind::Unary { op: UnOp::Neg, .. } => identity_or_bottom(state, truth),
        ExprKind::Binary { op: BinOp::And, lhs, rhs } => {
            if truth {
                let s = assume(state, lhs, true);
                assume(&s, rhs, true)
            } else {
                // ¬(a ∧ b) = ¬a ∨ ¬b — over-approximate the union.
                assume(state, lhs, false).join(&assume(state, rhs, false))
            }
        }
        ExprKind::Binary { op: BinOp::Or, lhs, rhs } => {
            if truth {
                assume(state, lhs, true).join(&assume(state, rhs, true))
            } else {
                let s = assume(state, lhs, false);
                assume(&s, rhs, false)
            }
        }
        ExprKind::Var(name) => refine_nonzero(state, name, truth),
        ExprKind::Binary { op, lhs, rhs } => {
            let Some(rel) = rel_of(*op) else {
                // Arithmetic used directly as a truth value: no interval cut
                // available, identity refinement.
                return state.clone();
            };
            let rel = if truth { rel } else { rel.flip() };
            // Canonicalise to affine LHS, constant RHS; swap operands and
            // turn the relation around when needed. Pure constants are
            // affine with an empty variable name.
            if let Some(a) = affine(lhs) {
                let rhs_const = if a.var.is_empty() {
                    // Constant-vs-anything: only cut when RHS is also a
                    // decidable constant.
                    const_of(rhs)
                } else {
                    affine_const(rhs)
                };
                match rhs_const {
                    Some(k) => cut_affine(state, &a, rel, k),
                    None => {
                        if a.var.is_empty() {
                            state.clone()
                        } else if let Some(iv) = interval_of(rhs, state) {
                            cut_affine_iv(state, &a, rel, iv)
                        } else {
                            state.clone()
                        }
                    }
                }
            } else if let Some(a) = affine(rhs) {
                let lhs_const = if a.var.is_empty() {
                    const_of(lhs)
                } else {
                    affine_const(lhs)
                };
                match lhs_const {
                    Some(k) => cut_affine(state, &a, swap_rel(rel), k),
                    None => {
                        if a.var.is_empty() {
                            state.clone()
                        } else if let Some(iv) = interval_of(lhs, state) {
                            cut_affine_iv(state, &a, swap_rel(rel), iv)
                        } else {
                            state.clone()
                        }
                    }
                }
            } else {
                state.clone()
            }
        }
        ExprKind::ArrayRead { .. } => identity_or_bottom(state, truth),
    }
}

fn identity_or_bottom(state: &AbsState, truth: bool) -> AbsState {
    // A non-boolean-structured value: truth might hold; falsity cannot be
    // proven either, so stay identity in both cases.
    let _ = truth;
    state.clone()
}

fn affine_const(e: &Expr) -> Option<i128> {
    match affine(e) {
        Some(_) => None,
        None => const_of(e),
    }
}

fn const_of(e: &Expr) -> Option<i128> {
    match &e.kind {
        ExprKind::Int(v) => Some(*v as i128),
        ExprKind::Unary { op: UnOp::Neg, inner } => Some(-const_of(inner)?),
        ExprKind::Binary { op, lhs, rhs } => {
            let a = const_of(lhs)?;
            let b = const_of(rhs)?;
            Some(match op {
                BinOp::Add => a + b,
                BinOp::Sub => a - b,
                BinOp::Mul => a * b,
                _ => return None,
            })
        }
        _ => None,
    }
}

fn refine_nonzero(state: &AbsState, name: &str, truth: bool) -> AbsState {
    let x = state.var(name);
    if truth {
        // x != 0: union of x <= -1 and x >= 1.
        let neg = x.meet(Interval::Range { lo: i64::MIN, hi: -1 });
        let pos = x.meet(Interval::Range { lo: 1, hi: i64::MAX });
        let mut out = state.clone();
        out.set_var(name, neg.join(pos));
        if neg.join(pos).is_bottom() {
            return AbsState::bottom();
        }
        out
    } else {
        // x == 0
        let cut = x.meet(Interval::ZERO);
        if cut.is_bottom() {
            return AbsState::bottom();
        }
        let mut out = state.clone();
        out.set_var(name, cut);
        out
    }
}

/// Small interval evaluator used to obtain the *bounds* of the non-affine
/// side of a comparison. It supports exactly the linear forms [`affine`]
/// handles (plus constant multiplication); anything else (arrays, division)
/// returns `None`, in which case the caller applies no cut. Failure flags are
/// irrelevant here: the cut is sound whenever a result is returned.
fn interval_of(e: &Expr, state: &AbsState) -> Option<Interval> {
    match &e.kind {
        ExprKind::Int(v) => Some(Interval::point(*v)),
        ExprKind::Var(name) => {
            let iv = state.var(name);
            iv.bounds().map(|_| iv)
        }
        ExprKind::ArrayRead { .. } => None,
        ExprKind::Unary { op: UnOp::Neg, inner } => {
            let iv = interval_of(inner, state)?;
            let (a, b) = iv.bounds()?;
            // (-b, -a) in exact arithmetic; only safe when MIN is excluded.
            let lo = (b as i128).checked_neg()?;
            let hi = (a as i128).checked_neg()?;
            if (i64::MIN as i128..=i64::MAX as i128).contains(&lo)
                && (i64::MIN as i128..=i64::MAX as i128).contains(&hi)
            {
                Some(Interval::Range {
                    lo: lo as i64,
                    hi: hi as i64,
                })
            } else {
                None
            }
        }
        ExprKind::Unary { op: UnOp::Not, .. } => Some(Interval::BOOL),
        ExprKind::Binary { op, lhs, rhs } => {
            let x = interval_of(lhs, state)?;
            let y = interval_of(rhs, state)?;
            let (iv, _) = ia_intervals::binary(*op, x, y);
            if iv.is_bottom() { None } else { Some(iv) }
        }
    }
}

/// Cut `coeff*var+offset rel [c, d]` using necessary bounds on the interval
/// side. Equality intersects; disequality cannot be expressed and is identity.
fn cut_affine_iv(state: &AbsState, a: &Affine, rel: Rel, rhs: Interval) -> AbsState {
    let (c, d) = match rhs.bounds() {
        Some(bd) => bd,
        None => return AbsState::bottom(),
    };
    // Translate the interval side by the affine offset:
    // coeff*x + offset rel [c, d]  ==>  coeff*x rel [c-offset, d-offset].
    let mut c1 = c as i128 - a.offset;
    let mut d1 = d as i128 - a.offset;
    let mut rel = rel;
    if a.coeff == -1 {
        // -x rel [c1, d1]  ==>  x rel' [-d1, -c1], with rel turned around.
        let (nc, nd) = (-d1, -c1);
        c1 = nc;
        d1 = nd;
        rel = match rel {
            Rel::Lt => Rel::Gt,
            Rel::Le => Rel::Ge,
            Rel::Gt => Rel::Lt,
            Rel::Ge => Rel::Le,
            Rel::Eq => Rel::Eq,
            Rel::Ne => Rel::Ne,
        };
    }
    // Intersect variable `name` with `[lo, hi]` (either side optional).
    let cut = |state: &AbsState, lo_bound: Option<i128>, hi_bound: Option<i128>| {
        cut_both(state, &a.var, lo_bound, hi_bound)
    };
    match rel {
        Rel::Lt => cut(state, None, Some(d1.saturating_sub(1))),
        Rel::Le => cut(state, None, Some(d1)),
        Rel::Gt => cut(state, Some(c1.saturating_add(1)), None),
        Rel::Ge => cut(state, Some(c1), None),
        Rel::Eq => cut(state, Some(c1), Some(d1)),
        Rel::Ne => state.clone(),
    }
}

/// Intersect variable `name` with `[lo, hi]` (either side optional), honoring
/// a `-1` coefficient by mirroring the bounds.
fn cut_both(state: &AbsState, name: &str, lo: Option<i128>, hi: Option<i128>) -> AbsState {
    let x = state.var(name);
    let Interval::Range { lo: xlo, hi: xhi } = x else {
        return AbsState::bottom();
    };
    // Normalise with coeff +1: when the coefficient is -1 the caller has
    // already flipped the relation; this helper receives post-flip bounds.
    let nlo = match lo {
        Some(b) if b <= i64::MAX as i128 => xlo.max(b.max(i64::MIN as i128) as i64),
        Some(_) => return AbsState::bottom(), // lower bound above MAX: empty
        None => xlo,
    };
    let nhi = match hi {
        Some(b) if b >= i64::MIN as i128 => xhi.min(b.min(i64::MAX as i128) as i64),
        Some(_) => return AbsState::bottom(), // upper bound below MIN: empty
        None => xhi,
    };
    match Interval::new(nlo, nhi) {
        Some(iv) if !iv.is_bottom() => {
            let mut out = state.clone();
            out.set_var(name, iv);
            out
        }
        _ => AbsState::bottom(),
    }
}

/// Apply relation `coeff*var+offset rel k` by cutting the variable's interval.
fn cut_affine(state: &AbsState, a: &Affine, rel: Rel, k: i128) -> AbsState {
    // Pure-constant comparison: decide it once, independent of any variable.
    if a.var.is_empty() {
        let lhs = a.offset;
        let holds = match rel {
            Rel::Lt => lhs < k,
            Rel::Le => lhs <= k,
            Rel::Gt => lhs > k,
            Rel::Ge => lhs >= k,
            Rel::Eq => lhs == k,
            Rel::Ne => lhs != k,
        };
        return if holds { state.clone() } else { AbsState::bottom() };
    }
    // Normalise to `x rel rhs` with coeff 1, flipping the relation if coeff -1.
    let mut rel = rel;
    let mut rhs = k - a.offset;
    if a.coeff == -1 {
        rhs = -rhs;
        rel = match rel {
            Rel::Lt => Rel::Gt,
            Rel::Le => Rel::Ge,
            Rel::Gt => Rel::Lt,
            Rel::Ge => Rel::Le,
            Rel::Eq => Rel::Eq,
            Rel::Ne => Rel::Ne,
        };
    }
    let x = state.var(&a.var);
    let Interval::Range { lo, hi } = x else {
        return AbsState::bottom();
    };
    let cut = match rel {
        Rel::Lt => bound_hi(hi, rhs.saturating_sub(1)).map(|nhi| Interval::new(lo, nhi).unwrap_or(Interval::BOTTOM)),
        Rel::Le => bound_hi(hi, rhs).map(|nhi| Interval::new(lo, nhi).unwrap_or(Interval::BOTTOM)),
        Rel::Gt => bound_lo(lo, rhs.saturating_add(1)).map(|nlo| Interval::new(nlo, hi).unwrap_or(Interval::BOTTOM)),
        Rel::Ge => bound_lo(lo, rhs).map(|nlo| Interval::new(nlo, hi).unwrap_or(Interval::BOTTOM)),
        Rel::Eq => eq_cut(lo, hi, rhs),
        Rel::Ne => ne_cut(lo, hi, rhs),
    };
    let Some(cut) = cut else {
        return state.clone(); // bound outside i64 range: not restrictive
    };
    if cut.is_bottom() {
        return AbsState::bottom();
    }
    let mut out = state.clone();
    out.set_var(&a.var, cut);
    out
}

/// Restrict hi to min(hi, bound) when bound <= i64::MAX; bound above MAX is
/// not restrictive; below MIN forces emptiness.
fn bound_hi(hi: i64, bound: i128) -> Option<i64> {
    if bound < i64::MIN as i128 {
        Some(i64::MIN) // interval becomes empty via new()
    } else if bound > i64::MAX as i128 {
        None
    } else {
        Some(hi.min(bound as i64))
    }
}

fn bound_lo(lo: i64, bound: i128) -> Option<i64> {
    if bound > i64::MAX as i128 {
        Some(i64::MAX) // empty via new()
    } else if bound < i64::MIN as i128 {
        None
    } else {
        Some(lo.max(bound as i64))
    }
}

fn eq_cut(lo: i64, hi: i64, rhs: i128) -> Option<Interval> {
    if !(i64::MIN as i128..=i64::MAX as i128).contains(&rhs) {
        return Some(Interval::BOTTOM);
    }
    let c = rhs as i64;
    Some(Interval::new(lo.max(c), hi.min(c)).unwrap_or(Interval::BOTTOM))
}

/// x != c splits into [lo, c-1] ∪ [c+1, hi]; result may be two pieces whose
/// hull we keep, since the domain cannot represent holes.
fn ne_cut(lo: i64, hi: i64, rhs: i128) -> Option<Interval> {
    if !(i64::MIN as i128..=i64::MAX as i128).contains(&rhs) {
        return Some(Interval::Range { lo, hi });
    }
    let c = rhs as i64;
    if c < lo || c > hi {
        return Some(Interval::Range { lo, hi });
    }
    let left = Interval::new(lo, c.saturating_sub(1)).unwrap_or(Interval::BOTTOM);
    let right = Interval::new(c.saturating_add(1), hi).unwrap_or(Interval::BOTTOM);
    Some(left.join(right))
}
