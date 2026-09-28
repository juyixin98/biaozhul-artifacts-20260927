//! Direct verification of a claimed P-invariant weight vector.
//!
//! Does not trust the generator: recomputes `y^T · C` column by column from
//! the net's own arc definitions and checks the weighted sum on supplied
//! markings.

use pn_core::{Net, Token};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InvariantVerdict {
    /// Length matches place count.
    pub right_dimension: bool,
    /// Every weight is non-negative (the candidate contract).
    pub non_negative: bool,
    /// `y^T · C = 0` for every transition.
    pub is_p_invariant: bool,
    /// Per-transition weighted change; all zero iff invariant.
    pub weighted_change: Vec<i128>,
    /// Weighted sums for each supplied marking (initial, target, ...).
    pub weighted_sums: Vec<i128>,
    /// Whether all supplied markings share one weighted sum. With one marking
    /// this is vacuously true; with two it is the necessary-condition check.
    pub equal_on_markings: bool,
    pub reasons: Vec<String>,
}

impl InvariantVerdict {
    pub fn valid_invariant(&self) -> bool {
        self.right_dimension && self.non_negative && self.is_p_invariant
    }
}

/// Verify `weights` as a P-invariant of `net`, then compare its weighted sum
/// across `markings` (typically initial then target).
pub fn verify_invariant_vector(
    net: &Net,
    weights: &[i128],
    markings: &[Vec<Token>],
) -> InvariantVerdict {
    let mut reasons = Vec::new();
    let right_dimension = weights.len() == net.place_count();
    if !right_dimension {
        reasons.push(format!(
            "weight vector length {} != place count {}",
            weights.len(),
            net.place_count()
        ));
    }
    let non_negative = weights.iter().all(|&w| w >= 0);
    if !non_negative {
        reasons.push("weight vector contains negative entries".to_string());
    }

    let mut weighted_change = Vec::new();
    let mut is_p_invariant = right_dimension && non_negative;
    if right_dimension {
        for tr in net.transitions() {
            let mut delta: i128 = 0;
            for a in &tr.inputs {
                let p = net.place_index(&a.place).expect("validated");
                delta -= weights[p] * a.weight as i128;
            }
            for a in &tr.outputs {
                let p = net.place_index(&a.place).expect("validated");
                delta += weights[p] * a.weight as i128;
            }
            weighted_change.push(delta);
            if delta != 0 {
                is_p_invariant = false;
            }
        }
        if !is_p_invariant && !weighted_change.iter().all(|&d| d == 0) {
            reasons.push(format!(
                "weighted change is not zero for every transition: {weighted_change:?}"
            ));
        }
    }

    let weighted_sums: Vec<i128> = markings
        .iter()
        .map(|m| {
            weights
                .iter()
                .zip(m.iter())
                .map(|(&w, &t)| w * t as i128)
                .sum()
        })
        .collect();
    let equal_on_markings = weighted_sums.windows(2).all(|w| w[0] == w[1]);

    InvariantVerdict {
        right_dimension,
        non_negative,
        is_p_invariant,
        weighted_change,
        weighted_sums,
        equal_on_markings,
        reasons,
    }
}
