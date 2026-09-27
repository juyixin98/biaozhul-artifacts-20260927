//! Solver kernel: symbolic state, SMT translation, and path exploration.

pub mod engine;
pub mod report;
pub mod state;
pub mod translator;

pub use engine::analyze;
pub use report::{AnalysisReport, ReplayMismatch, Verdict};
