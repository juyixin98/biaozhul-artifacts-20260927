//! Tunables for the abstract interpreter. Fixed versions live in the
//! workspace manifest; these are analysis knobs, not semantic choices.
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AnalyzerConfig {
    /// Apply bounded descending (narrowing) iterations after widening.
    pub narrowing: bool,
    /// Maximum descending iterations per loop.
    pub narrowing_iterations: usize,
    /// Maximum ascending iterations per loop before the widening cutoff is
    /// accepted as a (still sound) post-fixpoint.
    pub max_widen_iterations: usize,
    /// Skip widening and iterate plain Kleene chains (intended for tiny
    /// fixtures and unit tests).
    pub plain_fixpoint: bool,
    /// Cap on trace events per request, keeping reports readable.
    pub max_trace_events: usize,
}

impl Default for AnalyzerConfig {
    fn default() -> Self {
        Self {
            narrowing: true,
            narrowing_iterations: 8,
            max_widen_iterations: 64,
            plain_fixpoint: false,
            max_trace_events: 200,
        }
    }
}
