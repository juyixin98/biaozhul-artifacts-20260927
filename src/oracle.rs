//! Independent brute-force oracle — **test support only**.
//!
//! Enumerates every weak observable word up to a fixed depth using its own
//! closure fixpoint and BFS over macro-sets, and returns the shortest word on
//! which specification and implementation disagree on acceptance. It shares
//! only the compiled [`Pair`] type with the solver; none of the search logic is
//! reused, so equality between the oracle and the kernel answers is a real
//! cross-check rather than self-certification.

use std::collections::{HashMap, VecDeque};

use crate::compiler::SILENT;
use crate::model::{LabelId, Lts, Pair, StateId};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OracleVerdict {
    /// Shortest impl-only observable word (ids), empty = either divergence on
    /// epsilon or no divergence found within the depth bound; disambiguate via
    /// `impl_yes_spec_no` / `found`.
    pub shortest_diff: Vec<LabelId>,
    /// True when the implementation accepts but the specification does not
    /// (a real inclusion violation).
    pub impl_yes_spec_no: bool,
    /// Whether any inclusion violation was found within the depth bound.
    pub found: bool,
    pub explored: usize,
}

/// A reverse acceptance difference (spec accepts, implementation does not) is
/// *not* an inclusion violation and, crucially, must not stop the search: a
/// later word on the same BFS levels may still be impl-only.

fn closure(lts: &Lts, s: StateId) -> Vec<StateId> {
    let mut out = vec![s];
    let mut seen = vec![false; lts.state_count()];
    seen[s as usize] = true;
    let mut i = 0;
    while i < out.len() {
        let q = out[i];
        i += 1;
        for e in &lts.outgoing[q as usize] {
            if e.label == SILENT && !seen[e.target as usize] {
                seen[e.target as usize] = true;
                out.push(e.target);
            }
        }
    }
    out.sort_unstable();
    out
}

fn weak_post(lts: &Lts, closures: &[Vec<StateId>], set: &[StateId], a: LabelId) -> Vec<StateId> {
    let mut seen = vec![false; lts.state_count()];
    for &q in set {
        for e in &lts.outgoing[q as usize] {
            if e.label == a {
                for &t in &closures[e.target as usize] {
                    seen[t as usize] = true;
                }
            }
        }
    }
    seen.into_iter().zip(0..).filter_map(|(v, i)| v.then_some(i)).collect()
}

fn accepts(lts: &Lts, set: &[StateId]) -> bool {
    set.iter().any(|&s| lts.accepting[s as usize])
}

/// Exhaustively enumerate both sides' macro-set BFS to `max_depth` observable
/// steps. Returns the first (shortest, alphabetical) acceptance disagreement.
pub fn brute_force_diff(pair: &Pair, max_depth: usize) -> OracleVerdict {
    let spec_c: Vec<Vec<StateId>> = (0..pair.spec.state_count() as StateId)
        .map(|s| closure(&pair.spec, s))
        .collect();
    let impl_c: Vec<Vec<StateId>> = (0..pair.impl_.state_count() as StateId)
        .map(|s| closure(&pair.impl_, s))
        .collect();

    let spec0 = spec_c[pair.spec.initial as usize].clone();
    let impl0 = impl_c[pair.impl_.initial as usize].clone();

    struct QItem {
        spec: Vec<StateId>,
        impl_: Vec<StateId>,
        word: Vec<LabelId>,
        depth: usize,
    }

    let mut q: VecDeque<QItem> = VecDeque::new();
    let mut visited: HashMap<(Vec<StateId>, Vec<StateId>), ()> = HashMap::new();
    q.push_back(QItem {
        spec: spec0,
        impl_: impl0,
        word: Vec::new(),
        depth: 0,
    });
    let mut explored = 0usize;

    while let Some(item) = q.pop_front() {
        explored += 1;
        let sa = accepts(&pair.spec, &item.spec);
        let ia = accepts(&pair.impl_, &item.impl_);
        if ia && !sa {
            // Inclusion violation: implementation accepts a word the
            // specification cannot accept. BFS order makes it the shortest.
            return OracleVerdict {
                shortest_diff: item.word,
                impl_yes_spec_no: true,
                found: true,
                explored,
            };
        }
        // A reverse difference (sa && !ia) is not a violation; do not stop.
        if item.depth >= max_depth {
            continue;
        }
        for a in 0..pair.label_names.len() as LabelId {
            let ns = weak_post(&pair.spec, &spec_c, &item.spec, a);
            let ni = weak_post(&pair.impl_, &impl_c, &item.impl_, a);
            let key = (ns.clone(), ni.clone());
            if visited.contains_key(&key) {
                continue;
            }
            visited.insert(key, ());
            let mut word = item.word.clone();
            word.push(a);
            q.push_back(QItem {
                spec: ns,
                impl_: ni,
                word,
                depth: item.depth + 1,
            });
        }
    }

    OracleVerdict {
        shortest_diff: Vec::new(),
        impl_yes_spec_no: false,
        found: false,
        explored,
    }
}

/// True when no inclusion violation exists up to depth `max_depth`.
pub fn agrees_with_inclusion(pair: &Pair, max_depth: usize) -> bool {
    !brute_force_diff(pair, max_depth).found
}
