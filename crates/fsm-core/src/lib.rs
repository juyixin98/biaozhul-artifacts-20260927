//! # fsm-core
//!
//! Explicit-state search kernel.
//!
//! * Enumerates initial states by scanning the (finite) product domain and
//!   evaluating the initial predicate.
//! * Explores reachable states with breadth-first search, recording a parent
//!   edge for each newly discovered state. BFS plus one parent per state
//!   yields the **shortest** witness path measured in transitions.
//! * States are deduplicated with the bijective mixed-radix codec from
//!   [`fsm_lang::codec`].
//! * A hard budget bounds the number of distinct states consumed. Running
//!   past it yields [`Conclusion::Unknown`] with exploration statistics —
//!   never a false claim of proof.
//! * Deadlock (enabled-transition-free state that is *not* terminal) is
//!   detected independently of legal termination.

mod engine;
mod evidence;

pub use engine::{run_check, CheckOptions, Query, QueryKind, RunError, RunErrorKind, Stats};
pub use evidence::{Evidence, EvidenceKind, Step};

use serde::{Deserialize, Serialize};

/// Verdict for one property.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Conclusion {
    /// Property holds (no violation within the fully explored reachable set).
    Holds,
    /// Property is violated; `evidence` carries the witness.
    Violated,
    /// Search stopped at the budget; the property is neither proven nor
    /// refuted.
    Unknown,
}

/// Per-property result.
#[derive(Debug, Clone, Serialize)]
pub struct PropertyResult {
    pub name: String,
    pub kind: QueryKind,
    /// The property expression, for log explainability (empty for deadlock).
    #[serde(skip_serializing_if = "String::is_empty")]
    pub expr: String,
    pub conclusion: Conclusion,
    /// Machine-readable reason, always present for `unknown`/`violated`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence: Option<Evidence>,
}

/// Overall run outcome.
#[derive(Debug, Clone, Serialize)]
pub struct Outcome {
    pub status: RunStatus,
    pub properties: Vec<PropertyResult>,
    /// Deadlock finding, even when no explicit deadlock query was supplied.
    pub deadlock_found: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deadlock_evidence: Option<Evidence>,
    pub stats: Stats,
    /// Human- and machine-readable progress trail.
    pub trace: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<RunError>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    /// Reachability closed within budget; all properties decided.
    Complete,
    /// Budget exhausted; `unknown` properties may remain.
    Truncated,
    /// A hard error during enumeration/evaluation; results are absent.
    Error,
}

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
