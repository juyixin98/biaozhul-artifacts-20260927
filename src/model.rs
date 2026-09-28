//! Solver-side compiled representation of labeled transition systems.
//!
//! Input models use string names; the compiler (see `compiler`) interns them
//! into this dense form. `StateId` / `LabelId` are indices valid inside one
//! [`Pair`] — labels are shared between specification and implementation after
//! alphabet alignment.

use std::collections::HashMap;

/// Dense state index inside one LTS.
pub type StateId = u32;
/// Dense label index shared across the aligned pair.
pub type LabelId = u32;

/// A compiled finite LTS with an explicit accepting set.
#[derive(Debug, Clone)]
pub struct Lts {
    pub name: String,
    pub state_names: Vec<String>,
    /// Index of the single initial state.
    pub initial: StateId,
    /// Outgoing edges per state: `(label, target)`.
    pub outgoing: Vec<Vec<Edge>>,
    /// Indexed by state id.
    pub accepting: Vec<bool>,
    /// Number of observable labels in the shared alphabet.
    pub label_count: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Edge {
    pub label: LabelId,
    pub target: StateId,
}

/// A transition edge with globally unique id inside one LTS.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EdgeRef {
    /// Source state of the edge.
    pub source: StateId,
    pub edge_id: u32,
}

impl Lts {
    pub fn state_count(&self) -> usize {
        self.state_names.len()
    }

    pub fn edge_count(&self) -> usize {
        self.outgoing.iter().map(|v| v.len()).sum()
    }

    pub fn name_of_state(&self, s: StateId) -> &str {
        &self.state_names[s as usize]
    }

    /// Build a `state name -> id` lookup (used by the evidence verifier).
    pub fn state_index(&self) -> HashMap<&str, StateId> {
        self.state_names
            .iter()
            .enumerate()
            .map(|(i, n)| (n.as_str(), i as StateId))
            .collect()
    }

    /// Resolve an [`EdgeRef`] by linear scan with bounds checks.
    pub fn edge(&self, edge_ref: EdgeRef) -> Option<(LabelId, StateId)> {
        let edges = self.outgoing.get(edge_ref.source as usize)?;
        let e = edges.get(edge_ref.edge_id as usize)?;
        Some((e.label, e.target))
    }
}

/// Two LTSs compiled against one shared, aligned observable alphabet.
#[derive(Debug, Clone)]
pub struct Pair {
    pub spec: Lts,
    pub impl_: Lts,
    /// `LabelId -> observable action name`. Length equals each Lts's
    /// `label_count`.
    pub label_names: Vec<String>,
    /// Declared silent action name, normalized away (it has no LabelId).
    pub silent_name: String,
}

impl Pair {
    pub fn label_name(&self, l: LabelId) -> &str {
        &self.label_names[l as usize]
    }
}
