//! # se-engine
//!
//! Bounded symbolic execution over the [`se_lang`] input language.
//!
//! * [`symbolic`] lowers AST expressions into [`se_solver::Term`]s, collecting
//!   semantic guards (divisor non-zero, trap-mode overflow), path-conditioned inside
//!   `ite` branches.
//! * [`engine`] drives an explicit worklist of path states, asks every feasibility
//!   question to an injected [`se_solver::SmtSolver`], enforces loop-unroll/path
//!   budgets, and emits candidate evidence plus an auditable event log.
//!
//! The engine never verifies its own evidence: replay against the independent concrete
//! interpreter is the job of the `se-verify` crate.

pub mod engine;
pub mod report;
pub mod symbolic;

pub use engine::{Engine, EngineConfig, ENGINE_VERSION};
pub use report::{
    AnalysisReport, BudgetUse, Cut, CutKind, Evidence, PathRecord, PathStatus,
    StepEvent, Verdict,
};
pub use se_lang::interp::FailureKind;
