//! `pn-solver`: analysis orchestration.
//!
//! Combines the typed input with the kernel's exhaustive bounded search and a
//! Farkas-style P-invariant candidate generator. It produces plain (serde
//! free) result types; the web layer maps them to wire DTOs.

pub mod analyze;
pub mod incidence;
pub mod invariant;

pub use analyze::{
    analyze, analyze_with_config, AnalysisOutcome, DeadlockMarking, InvariantCheck, ScopeNotice,
    TargetOutcome, WitnessFire,
};
pub use incidence::{incidence_matrix, IncidenceMatrix};
pub use invariant::{farkas_p_invariants, InvariantCandidate};
