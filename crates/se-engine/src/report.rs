//! Public result types produced by symbolic exploration.
//!
//! These are serializable so the HTTP layer can return them verbatim; they also carry
//! enough detail (conditions, trace events, budget accounting) for independent tests
//! to assert on *concrete* findings rather than on mere API reachability.

use std::collections::BTreeMap;

use serde::Serialize;
use se_lang::interp::FailureKind;
use se_solver::CheckStatus;

/// What the analysis concluded overall.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// At least one feasible violating path was found and (later) replayed.
    Violation,
    /// All explored paths completed safely *and* exploration was exhaustive within
    /// the configured budgets with no unknown cuts.
    Holds,
    /// No violation found, but coverage is incomplete (budget cut, unroll cut, or a
    /// solver `unknown`). Never silently reported as safe.
    Unknown,
}

impl Verdict {
    pub fn as_str(self) -> &'static str {
        match self {
            Verdict::Violation => "violation",
            Verdict::Holds => "holds",
            Verdict::Unknown => "unknown",
        }
    }
}

impl Serialize for Verdict {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathStatus {
    Safe,
    Violation,
    Infeasible,
    InfeasibleAssume,
    Unknown,
}

impl PathStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            PathStatus::Safe => "safe",
            PathStatus::Violation => "violation",
            PathStatus::Infeasible => "infeasible",
            PathStatus::InfeasibleAssume => "infeasible_assume",
            PathStatus::Unknown => "unknown",
        }
    }
}

impl Serialize for PathStatus {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}

/// Why an unfinished path was cut (always contributes to an overall `Unknown`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Cut {
    pub kind: CutKind,
    pub stmt_id: Option<usize>,
    pub detail: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CutKind {
    /// Worklist exceeded `max_paths`.
    PathBudget,
    /// A while-loop reached `max_loop_unroll` back-edges.
    LoopUnroll,
    /// Solver returned unknown/timeout on a feasibility query.
    SolverUnknown,
    /// Solver backend could not be executed.
    SolverUnavailable,
}

impl CutKind {
    pub fn as_str(self) -> &'static str {
        match self {
            CutKind::PathBudget => "path_budget",
            CutKind::LoopUnroll => "loop_unroll",
            CutKind::SolverUnknown => "solver_unknown",
            CutKind::SolverUnavailable => "solver_unavailable",
        }
    }
}

impl Serialize for CutKind {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}

/// A candidate violating input. Evidence is *not* trusted until independently replayed.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Evidence {
    pub failure: FailureKind,
    pub stmt_id: usize,
    pub op: Option<String>,
    pub inputs: BTreeMap<String, u64>,
    pub solver: String,
    pub solver_version: String,
    /// Conjunct path condition text (SMT-LIB), for auditability.
    pub path_condition: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct PathRecord {
    pub index: usize,
    pub status: String,
    pub terminal_stmt: Option<usize>,
    /// Raw text of each path-condition conjunct, in accumulation order.
    pub condition: Vec<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct BudgetUse {
    pub max_paths: usize,
    pub explored_terminals: usize,
    pub forked_branches: usize,
    pub solver_queries: usize,
    pub sat_queries: usize,
    pub unsat_queries: usize,
    pub unknown_queries: usize,
    pub max_loop_unroll: u32,
    pub max_unroll_observed: u32,
}

/// Human-auditable progress/decision log for one run.
#[derive(Clone, Debug, Serialize)]
pub struct StepEvent {
    pub seq: usize,
    pub kind: String,
    pub stmt_id: Option<usize>,
    pub detail: String,
    pub solver: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct AnalysisReport {
    pub engine_version: String,
    pub verdict: String,
    pub width_bits: u32,
    pub overflow: String,
    pub inputs: Vec<String>,
    pub stmt_count: usize,
    pub domain_size: String,
    pub budget: BudgetUse,
    pub evidence: Vec<Evidence>,
    pub cuts: Vec<Cut>,
    /// Per-path records (capped; see `paths_truncated`).
    pub paths: Vec<PathRecord>,
    pub paths_truncated: bool,
    pub steps: Vec<StepEvent>,
    pub steps_truncated: bool,
    /// Propagates the last non-OK solver note, if any.
    pub solver_note: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Check-status summary helper used by the engine and tests.
#[must_use]
pub fn status_name(s: CheckStatus) -> &'static str {
    match s {
        CheckStatus::Sat => "sat",
        CheckStatus::Unsat => "unsat",
        CheckStatus::Unknown => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cut_kind_serializes_snake_case() {
        for (k, txt) in [
            (CutKind::PathBudget, "path_budget"),
            (CutKind::LoopUnroll, "loop_unroll"),
            (CutKind::SolverUnknown, "solver_unknown"),
            (CutKind::SolverUnavailable, "solver_unavailable"),
        ] {
            assert_eq!(serde_json::to_string(&k).unwrap(), format!("\"{txt}\""));
        }
    }

    #[test]
    fn verdict_and_path_status_strings_are_stable() {
        assert_eq!(Verdict::Violation.as_str(), "violation");
        assert_eq!(Verdict::Holds.as_str(), "holds");
        assert_eq!(Verdict::Unknown.as_str(), "unknown");
        assert_eq!(
            serde_json::to_string(&PathStatus::InfeasibleAssume).unwrap(),
            "\"infeasible_assume\""
        );
    }
}
