//! Interval abstract-interpretation solver for bounded-i64 IAL programs.
//!
//! * [`analyzer::analyze`] runs the syntax-directed transfer function;
//! * loops are handled by an ascending chain with jump-to-bound widening,
//!   optionally followed by a bounded descending narrowing chain
//!   ([`state::AbsState`] / [`analyzer`]);
//! * branches are refined with sound affine condition narrowing
//!   ([`refine::assume`]);
//! * every check site is reported with one of the verdicts in
//!   [`report::CheckVerdict`], never conflating "possible" with "certain".

pub mod analyzer;
pub mod config;
pub mod refine;
pub mod report;
pub mod state;

pub use analyzer::analyze;
pub use config::AnalyzerConfig;
pub use report::{
    AnalysisReport, CheckKind, CheckRecord, CheckVerdict, FailureCertainty, FixpointStats,
    FixpointStrategy, TraceEvent, VerdictCounts,
};
pub use state::{AbsState, ArrayDomain};

/// Semantic version of the solver, embedded in reports.
pub const SOLVER_VERSION: &str = env!("CARGO_PKG_VERSION");
