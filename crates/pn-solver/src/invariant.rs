//! P-invariant candidates via the Farkas (Martinez–Silva style) row algorithm
//! on `[C | I]`, where `C` is the place×transition incidence matrix.
//!
//! A place-weight vector `y >= 0` is a P-invariant when `y^T · C = 0`, i.e.
//! every transition preserves the weighted token sum `y · m`. Candidates
//! produced here are re-validated independently by `pn-verify`; this generator
//! is an optimisation aid for pruning reachability, never an oracle.

use crate::incidence::IncidenceMatrix;

/// A non-negative P-invariant candidate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InvariantCandidate {
    /// Per-place non-negative integer weight, divided by the vector GCD.
    pub weights: Vec<i128>,
    /// Raw (pre-normalisation) weights are not retained; `gcd` is always 1.
    pub gcd: i128,
    /// True when no other candidate in the same generated set has a strictly
    /// contained support. Relative to the generated set only — this is not a
    /// claim that the full minimal S-invariant basis was enumerated.
    pub support_minimal_in_set: bool,
    /// Places carrying non-zero weight.
    pub support: Vec<usize>,
}

/// Output of a candidate-generation run.
#[derive(Debug, Clone)]
pub struct FarkasResult {
    pub candidates: Vec<InvariantCandidate>,
    /// True if the internal row budget was exhausted; candidates may then be
    /// incomplete. Reported rather than silently hidden.
    pub truncated: bool,
}

/// Row budget preventing exponential intermediate blow-up.
const DEFAULT_ROW_BUDGET: usize = 8_192;

pub fn farkas_p_invariants(c: &IncidenceMatrix) -> FarkasResult {
    farkas_p_invariants_bounded(c, DEFAULT_ROW_BUDGET)
}

pub fn farkas_p_invariants_bounded(c: &IncidenceMatrix, row_budget: usize) -> FarkasResult {
    let p = c.place_count;
    let t = c.transition_count;
    let width = t + p;

    // Working matrix [C | I]: C occupies columns 0..t, identity t..t+p.
    let mut rows: Vec<Vec<i128>> = Vec::with_capacity(p);
    for i in 0..p {
        let mut row = vec![0i128; width];
        row[..t].copy_from_slice(&c.rows[i]);
        row[t + i] = 1;
        rows.push(row);
    }

    let mut truncated = false;

    for col in 0..t {
        // Partition current rows by their entry in this transition column.
        let mut zero_rows: Vec<Vec<i128>> = Vec::new();
        let mut pos: Vec<Vec<i128>> = Vec::new();
        let mut neg: Vec<Vec<i128>> = Vec::new();
        for row in rows.drain(..) {
            match row[col].cmp(&0) {
                std::cmp::Ordering::Equal => zero_rows.push(row),
                std::cmp::Ordering::Greater => pos.push(row),
                std::cmp::Ordering::Less => neg.push(row),
            }
        }

        let mut next: Vec<Vec<i128>> = zero_rows;

        // Martinez–Silva annihilation: for every (positive, negative) pair
        // form the LCM-normalised sum that zeroes the column. Rows that alone
        // carry a sign (no counterpart) cannot be annihilated here and are
        // carried forward unchanged.
        if pos.is_empty() || neg.is_empty() {
            next.extend(pos);
            next.extend(neg);
        } else {
            for pr in &pos {
                for nr in &neg {
                    let (a, b) = (pr[col], nr[col]); // a > 0, b < 0
                    let l = lcm(a, b.unsigned_abs() as i128);
                    let (cf, cg) = (l / a, l / b.abs());
                    let combined: Vec<i128> = pr
                        .iter()
                        .zip(nr.iter())
                        .map(|(&x, &y)| cf * x + cg * y)
                        .collect();
                    debug_assert_eq!(combined[col], 0);
                    next.push(combined);
                }
            }
        }

        if next.len() > row_budget {
            truncated = true;
            next.truncate(row_budget);
        }
        rows = next;
        if truncated {
            break;
        }
    }

    // Keep rows whose incidence part is all zero: they are P-invariants.
    // Non-negativity is enforced on the *final* weight vector, never on
    // intermediates, so a genuine non-negative invariant cannot be dropped.
    let mut raw: Vec<Vec<i128>> = rows
        .into_iter()
        .filter(|r| r[0..t].iter().all(|&x| x == 0))
        .map(|r| r[t..t + p].to_vec())
        .filter(|w| w.iter().any(|&x| x != 0))
        .filter(|w| w.iter().all(|&x| x >= 0))
        .collect();

    // Remove a candidate if it is a positive integer multiple of another
    // (keeps the minimal generators while preserving non-minimal invariants
    // that no single generator divides).
    // Normalise by GCD first, then dedup.
    for w in raw.iter_mut() {
        let g = w.iter().fold(0i128, |acc, &x| gcd(acc, x.unsigned_abs() as i128));
        if g > 1 {
            for x in w.iter_mut() {
                *x /= g;
            }
        }
    }
    raw.sort();
    raw.dedup();

    let candidates: Vec<InvariantCandidate> = raw
        .iter()
        .map(|w| {
            let support: Vec<usize> = w
                .iter()
                .enumerate()
                .filter_map(|(i, &x)| (x > 0).then_some(i))
                .collect();
            InvariantCandidate {
                weights: w.clone(),
                gcd: 1,
                support_minimal_in_set: false,
                support,
            }
        })
        .collect();

    // Mark support-minimality within the generated set.
    let mut with_flags = candidates;
    for i in 0..with_flags.len() {
        let si: std::collections::HashSet<usize> =
            with_flags[i].support.iter().copied().collect();
        let minimal = !with_flags.iter().enumerate().any(|(j, other)| {
            if i == j || other.support.len() >= si.len() {
                return false;
            }
            other.support.iter().all(|p| si.contains(p))
        });
        with_flags[i].support_minimal_in_set = minimal;
    }

    FarkasResult {
        candidates: with_flags,
        truncated,
    }
}

fn gcd(a: i128, b: i128) -> i128 {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}

/// Least common multiple of two positive magnitudes.
fn lcm(a: i128, b: i128) -> i128 {
    (a / gcd(a, b)) * b
}

#[cfg(test)]
mod tests {
    use super::*;
    use pn_core::{ArcDef, PlaceDef, TransitionDef};

    fn net(trans: Vec<TransitionDef>, places: Vec<PlaceDef>) -> pn_core::Net {
        let n = places.len();
        pn_core::Net::new(places, trans, vec![0; n]).unwrap()
    }

    #[test]
    fn mutex_yields_resource_invariant() {
        // idle <-> busy around one token resource; invariant idle+busy=1.
        let n = net(
            vec![
                TransitionDef {
                    name: "acq".into(),
                    inputs: vec![ArcDef { place: "idle".into(), weight: 1 }],
                    outputs: vec![ArcDef { place: "busy".into(), weight: 1 }],
                },
                TransitionDef {
                    name: "rel".into(),
                    inputs: vec![ArcDef { place: "busy".into(), weight: 1 }],
                    outputs: vec![ArcDef { place: "idle".into(), weight: 1 }],
                },
            ],
            vec![
                PlaceDef { name: "idle".into(), capacity: 1 },
                PlaceDef { name: "busy".into(), capacity: 1 },
            ],
        );
        let c = crate::incidence_matrix(&n);
        let res = farkas_p_invariants(&c);
        assert!(
            res.candidates.iter().any(|inv| inv.weights == vec![1, 1]),
            "expected (1,1) candidate, got {:?}",
            res.candidates
        );
        for cand in &res.candidates {
            assert_eq!(c.weighted_change(&cand.weights), vec![0, 0]);
        }
    }

    #[test]
    fn weighted_arc_invariant() {
        // One t moves 2 tokens a -> 1 token b via a second transition cycle;
        // build producer/consumer weights y with 2*y_a = y_b conserving.
        let n = net(
            vec![
                TransitionDef {
                    name: "make".into(),
                    inputs: vec![ArcDef { place: "a".into(), weight: 2 }],
                    outputs: vec![ArcDef { place: "b".into(), weight: 1 }],
                },
                TransitionDef {
                    name: "undo".into(),
                    inputs: vec![ArcDef { place: "b".into(), weight: 1 }],
                    outputs: vec![ArcDef { place: "a".into(), weight: 2 }],
                },
            ],
            vec![
                PlaceDef { name: "a".into(), capacity: 4 },
                PlaceDef { name: "b".into(), capacity: 4 },
            ],
        );
        let c = crate::incidence_matrix(&n);
        let res = farkas_p_invariants(&c);
        assert!(res.candidates.iter().any(|inv| inv.weights == vec![1, 2]));
        for cand in &res.candidates {
            assert!(c.weighted_change(&cand.weights).iter().all(|&x| x == 0));
        }
    }

    #[test]
    fn two_independent_resources_yield_two_minimal_invariants() {
        // Two disjoint mutex resources; both minimal P-semiflows must appear,
        // which a single-pivot annihilation can miss on some nets.
        let n = net(
            vec![
                TransitionDef {
                    name: "acq1".into(),
                    inputs: vec![ArcDef { place: "i1".into(), weight: 1 }],
                    outputs: vec![ArcDef { place: "b1".into(), weight: 1 }],
                },
                TransitionDef {
                    name: "rel1".into(),
                    inputs: vec![ArcDef { place: "b1".into(), weight: 1 }],
                    outputs: vec![ArcDef { place: "i1".into(), weight: 1 }],
                },
                TransitionDef {
                    name: "acq2".into(),
                    inputs: vec![ArcDef { place: "i2".into(), weight: 1 }],
                    outputs: vec![ArcDef { place: "b2".into(), weight: 1 }],
                },
                TransitionDef {
                    name: "rel2".into(),
                    inputs: vec![ArcDef { place: "b2".into(), weight: 1 }],
                    outputs: vec![ArcDef { place: "i2".into(), weight: 1 }],
                },
            ],
            vec![
                PlaceDef { name: "i1".into(), capacity: 1 },
                PlaceDef { name: "b1".into(), capacity: 1 },
                PlaceDef { name: "i2".into(), capacity: 1 },
                PlaceDef { name: "b2".into(), capacity: 1 },
            ],
        );
        let c = crate::incidence_matrix(&n);
        let res = farkas_p_invariants(&c);
        assert!(res.candidates.iter().any(|i| i.weights == vec![1, 1, 0, 0]));
        assert!(res.candidates.iter().any(|i| i.weights == vec![0, 0, 1, 1]));
        for cand in &res.candidates {
            assert!(c.weighted_change(&cand.weights).iter().all(|&x| x == 0));
        }
    }
}
