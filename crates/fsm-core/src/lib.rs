//! # fsm-core
//!
//! Explicit-state BFS model-checking kernel for specifications compiled by
//! [`fsm-lang`]. Produces AG/EF verdicts, shortest counterexample evidence,
//! deadlock/terminal traces, exploration statistics and explicit
//! truncation handling.

pub mod explorer;

pub use explorer::{
    explore, Budget, ExploreOutcome, PropertyResult, RunError, RunStatus, Stats, Verdict,
};
pub use explorer::reason;
