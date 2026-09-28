//! Pluggable SAT solver kernel.
//!
//! The extraction core depends only on the [`SatSolver`] trait, never on a concrete
//! solver. Three implementations ship in this crate:
//!
//! - [`dpll::DpllSolver`] — an in-process DPLL solver used by default.
//! - [`brute::BruteForceSolver`] — an independent truth-table solver used as the
//!   default *verifier*. It shares no search code with DPLL on purpose: evidence must
//!   not be produced by the implementation under test.
//! - [`external::ExternalSolver`] — adapts any DIMACS/`s SATISFIABLE` CLI solver
//!   (e.g. kissat/minisat-style binaries) over std::process.
//!
//! [`registry::SolverRegistry`] constructs implementations from configuration, which
//! is what makes the kernel "replaceable": swapping solver is a config/request choice,
//! not a code change in the extraction algorithm.

pub mod brute;
pub mod dpll;
pub mod external;
pub mod registry;

use std::sync::atomic::AtomicBool;

use crate::language::Formula;

/// Per-membership selection over `formula.clauses`: true = clause participates.
pub type ClauseMask = Vec<bool>;

/// The three solver verdicts. **`Unknown` is never treated as `Unsat`**: a solver may
/// time out, hit a decision budget or report an internal failure, and each of those is
/// carried explicitly through every layer of the service.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SolveStatus {
    Sat,
    Unsat,
    Unknown,
}

impl SolveStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            SolveStatus::Sat => "sat",
            SolveStatus::Unsat => "unsat",
            SolveStatus::Unknown => "unknown",
        }
    }
}

/// Per-call resource limits. The *number* of solver calls an extraction may make is
/// enforced by the extraction core; these limits apply to one invocation.
#[derive(Debug, Clone, Default)]
pub struct SolveLimits {
    /// Maximum branching decisions for search-based in-process solvers.
    pub max_decisions: Option<u64>,
    /// Wall-clock timeout, primarily for the external CLI adapter.
    pub timeout_ms: Option<u64>,
}

/// Result of one solver call. `model` is present iff `status == Sat` and maps each
/// active variable to a truth value; the extraction layer treats it as an opaque
/// witness for the verifier.
#[derive(Debug, Clone)]
pub struct SolveOutcome {
    pub status: SolveStatus,
    pub model: Option<std::collections::BTreeMap<i64, bool>>,
    pub decisions: u64,
    pub reason: Option<String>,
}

impl SolveOutcome {
    pub fn sat(model: std::collections::BTreeMap<i64, bool>, decisions: u64) -> Self {
        Self {
            status: SolveStatus::Sat,
            model: Some(model),
            decisions,
            reason: None,
        }
    }

    pub fn unsat(decisions: u64) -> Self {
        Self {
            status: SolveStatus::Unsat,
            model: None,
            decisions,
            reason: None,
        }
    }

    pub fn unknown(reason: impl Into<String>, decisions: u64) -> Self {
        Self {
            status: SolveStatus::Unknown,
            model: None,
            decisions,
            reason: Some(reason.into()),
        }
    }
}

/// A replaceable SAT solver.
///
/// Implementations must be deterministic for reproducibility: identical inputs,
/// limits and cancellation state must produce the same verdict.
pub trait SatSolver: Send + Sync {
    fn name(&self) -> &str;

    fn solve(
        &self,
        formula: &Formula,
        mask: &[bool],
        limits: &SolveLimits,
        cancel: Option<&AtomicBool>,
    ) -> SolveOutcome;
}

/// Iterate references to the clauses selected by `mask`.
pub fn active_clauses<'a>(
    formula: &'a Formula,
    mask: &'a [bool],
) -> impl Iterator<Item = &'a crate::language::Clause> + 'a {
    formula
        .clauses
        .iter()
        .zip(mask.iter())
        .filter(|(_, on)| **on)
        .map(|(c, _)| c)
}

/// Largest variable id appearing in the active subset (0 if empty).
pub fn active_max_var(formula: &Formula, mask: &[bool]) -> i64 {
    let mut max_var = 0;
    for c in active_clauses(formula, mask) {
        for lit in &c.literals {
            max_var = max_var.max(lit.var.0);
        }
    }
    max_var
}
