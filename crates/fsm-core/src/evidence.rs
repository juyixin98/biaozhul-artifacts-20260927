//! Witness data: a concrete replayable path with per-state variable values
//! and the transition that moves between consecutive states.

use serde::Serialize;

use fsm_lang::{render_value, System, Value};

/// What a witness demonstrates.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EvidenceKind {
    /// Path ending in a state where the AG invariant is false.
    AgViolation,
    /// Path ending in a state satisfying the EF target.
    EfReachable,
    /// Path ending in a non-terminal state with no enabled transition.
    Deadlock,
}

/// One edge of the path.
#[derive(Debug, Clone, Serialize)]
pub struct Step {
    pub index: usize,
    /// Variable assignment after arriving at this state (the path's first
    /// step already renders the initial state).
    pub state: serde_json::Map<String, serde_json::Value>,
    /// Transition fired to reach `state`; absent on the first step.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fired: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Evidence {
    pub kind: EvidenceKind,
    /// Number of transitions on the path (`states.len() - 1`).
    pub length: usize,
    pub path: Vec<Step>,
}

impl Evidence {
    /// Build an evidence path from a parent-link reconstruction.
    /// `chain` is `(state, fired)` from target back to the root; the root has
    /// `fired == None`.
    pub fn from_chain(
        kind: EvidenceKind,
        sys: &System,
        chain: Vec<(Vec<Value>, Option<String>)>,
    ) -> Self {
        let mut path = Vec::with_capacity(chain.len());
        for (i, (st, fired)) in chain.into_iter().enumerate() {
            path.push(Step {
                index: i,
                state: sys
                    .vars
                    .iter()
                    .zip(&st)
                    .map(|(v, val)| (v.name.clone(), render_value(&v.domain, *val)))
                    .collect(),
                fired,
            });
        }
        Evidence {
            kind,
            length: path.len().saturating_sub(1),
            path,
        }
    }
}
