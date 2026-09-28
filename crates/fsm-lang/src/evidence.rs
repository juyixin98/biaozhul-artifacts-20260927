//! Evidence produced by the kernel and independently replayed by the
//! `fsm-evidence` crate. The structures live here so both sides share the
//! serialization contract without the verifier depending on kernel logic.

use crate::model::Value;
use serde::{Deserialize, Serialize};

/// Classification of the finding a piece of evidence supports.
pub mod kind {
    pub const AG_VIOLATION: &str = "ag_violation";
    pub const EF_WITNESS: &str = "ef_witness";
    pub const DEADLOCK: &str = "deadlock";
    pub const TERMINAL: &str = "terminal";
}

/// One labeled state on a trace.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TraceStep {
    /// 0-based position in the trace.
    pub index: usize,
    /// Full variable assignment, in specification declaration order.
    pub state: Vec<NamedValue>,
    /// Name of the transition fired to reach this state, or null for the
    /// initial (root) state.
    pub fired: Option<String>,
}

/// A variable/value pair in a trace.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NamedValue {
    pub var: String,
    pub value: Value,
}

/// A concrete trace supporting a verdict.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Evidence {
    pub kind: String,
    /// Name of the property this evidence pertains to (empty for
    /// deadlock/terminal traces).
    #[serde(default)]
    pub property: String,
    pub trace: Vec<TraceStep>,
}
