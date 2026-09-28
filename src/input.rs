//! Input language: JSON shape accepted by the checker.
//!
//! ```json
//! {
//!   "silent_action": "tau",
//!   "alphabet": ["a", "b"],
//!   "specification": { ...LtsDef... },
//!   "implementation": { ...LtsDef... },
//!   "limits": { ... }
//! }
//! ```
//!
//! All names are arbitrary non-empty strings. A transition with the silent
//! action label is an internal step; every other label must be observable.
//! When `alphabet` is omitted the observable alphabet is the union of the
//! observable actions of both LTSs — but alphabet alignment is still explicit
//! in the response (`aligned_alphabet`) and an LTS may *not* use a label the
//! other side declares (declarations do not have to be equal; actions usable
//! by the implementation but absent from the specification simply make
//! inclusion false and surface as the shortest counterexample).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CheckRequest {
    /// Name of the internal silent action. Defaults to `"tau"`.
    #[serde(default = "default_silent")]
    pub silent_action: String,
    /// Explicit shared observable alphabet. If omitted it is derived as the
    /// union of both sides' observable actions, sorted for determinism.
    #[serde(default)]
    pub alphabet: Option<Vec<String>>,
    pub specification: LtsDef,
    pub implementation: LtsDef,
    #[serde(default)]
    pub limits: Option<LimitsDef>,
}

fn default_silent() -> String {
    "tau".to_string()
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LtsDef {
    pub name: String,
    pub initial: String,
    /// Explicit state list. When empty it is derived from transition endpoints
    /// plus the initial state.
    #[serde(default)]
    pub states: Vec<String>,
    /// Accepting states. Omitted / null means **every** state is accepting
    /// (plain trace semantics). An empty list means no state accepts.
    #[serde(default)]
    pub accepting: Option<Vec<String>>,
    #[serde(default)]
    pub transitions: Vec<TransitionDef>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TransitionDef {
    pub from: String,
    pub action: String,
    pub to: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LimitsDef {
    /// Refuse to compile inputs with more states than this (per LTS).
    #[serde(default = "default_max_states")]
    pub max_states_per_lts: usize,
    /// Refuse to compile inputs with more transitions than this (per LTS).
    #[serde(default = "default_max_transitions")]
    pub max_transitions_per_lts: usize,
    /// Determinization / antichain BFS: stop after this many explored pairs.
    #[serde(default = "default_max_pairs")]
    pub max_explored_pairs: usize,
    /// Cap on the number of edges a single replay witness may contain.
    #[serde(default = "default_max_witness_edges")]
    pub max_witness_edges: usize,
}

fn default_max_states() -> usize {
    100_000
}
fn default_max_transitions() -> usize {
    500_000
}
fn default_max_pairs() -> usize {
    2_000_000
}
fn default_max_witness_edges() -> usize {
    1_000_000
}

impl Default for LimitsDef {
    fn default() -> Self {
        Self {
            max_states_per_lts: default_max_states(),
            max_transitions_per_lts: default_max_transitions(),
            max_explored_pairs: default_max_pairs(),
            max_witness_edges: default_max_witness_edges(),
        }
    }
}

impl CheckRequest {
    pub fn limits(&self) -> LimitsDef {
        self.limits.clone().unwrap_or_default()
    }
}
