//! Integer incidence matrix over the net.
//!
//! `C[p, t] = out(p, t) - in(p, t)`: the net token change of place `p` when
//! transition `t` fires once.

use pn_core::Net;

/// Rows indexed by place, columns by transition.
#[derive(Debug, Clone)]
pub struct IncidenceMatrix {
    pub rows: Vec<Vec<i128>>,
    pub place_count: usize,
    pub transition_count: usize,
}

impl IncidenceMatrix {
    pub fn entry(&self, place: usize, transition: usize) -> i128 {
        self.rows[place][transition]
    }

    /// `y^T · C`: weighted token change vector of place-weight vector `y`.
    pub fn weighted_change(&self, y: &[i128]) -> Vec<i128> {
        (0..self.transition_count)
            .map(|t| {
                self.rows
                    .iter()
                    .zip(y.iter())
                    .map(|(row, &w)| w * row[t])
                    .sum()
            })
            .collect()
    }

    /// Weighted token sum `y · m` of a (non-negative) marking.
    pub fn weighted_sum(&self, y: &[i128], marking: &[u64]) -> i128 {
        y.iter()
            .zip(marking.iter())
            .map(|(&w, &m)| w * m as i128)
            .sum()
    }
}

pub fn incidence_matrix(net: &Net) -> IncidenceMatrix {
    let p = net.place_count();
    let t = net.transition_count();
    let mut rows = vec![vec![0i128; t]; p];
    for (ti, tr) in net.transitions().iter().enumerate() {
        for a in &tr.inputs {
            let pi = net.place_index(&a.place).expect("validated");
            rows[pi][ti] -= a.weight as i128;
        }
        for a in &tr.outputs {
            let pi = net.place_index(&a.place).expect("validated");
            rows[pi][ti] += a.weight as i128;
        }
    }
    IncidenceMatrix {
        rows,
        place_count: p,
        transition_count: t,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use pn_core::{ArcDef, PlaceDef, TransitionDef};

    #[test]
    fn incidence_signs() {
        let net = Net::new(
            vec![
                PlaceDef { name: "a".into(), capacity: 5 },
                PlaceDef { name: "b".into(), capacity: 5 },
            ],
            vec![TransitionDef {
                name: "t".into(),
                inputs: vec![ArcDef { place: "a".into(), weight: 2 }],
                outputs: vec![ArcDef { place: "b".into(), weight: 3 }],
            }],
            vec![2, 0],
        )
        .unwrap();
        let c = incidence_matrix(&net);
        assert_eq!(c.entry(0, 0), -2);
        assert_eq!(c.entry(1, 0), 3);
    }
}
