//! Analysis result types.
//!
//! Unknown is a first-class outcome: any path the engine could not fully
//! explore (solver timeout, loop unrolling cap, path budget) is reported as
//! unknown and forces the overall verdict to `unknown`, never `safe`.

use std::collections::BTreeMap;

use serde::Serialize;

use crate::config::EngineConfig;
use crate::evidence::concrete::FailureKind as ConcKind;
use crate::evidence::native::{NBool, SsaEnv};
use crate::evidence::replay::ReplayCheck;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureKind {
    AssertionFailed,
    DivisionByZero,
}

impl From<FailureKind> for ConcKind {
    fn from(k: FailureKind) -> Self {
        match k {
            FailureKind::AssertionFailed => ConcKind::AssertionFailed,
            FailureKind::DivisionByZero => ConcKind::DivisionByZero,
        }
    }
}

/// How one branch decision resolved under the current path condition.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BranchFeasibility {
    Feasible,
    Infeasible,
    /// Solver returned unknown; treated as potentially feasible.
    Unknown,
}

#[derive(Debug, Clone, Serialize)]
pub struct PathBranch {
    pub node_id: u32,
    pub line: u32,
    pub taken: bool,
    pub feasibility: BranchFeasibility,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PathStatus {
    /// Path completed without violating an assertion.
    Safe,
    /// Path ends in a reproduced failure.
    Failed,
    /// Branch / assumption was proved infeasible; path does not exist.
    Infeasible,
    /// A solver query returned unknown on this path.
    UnknownSolver,
    /// Exploration stopped because the loop unrolling bound was hit.
    IncompleteUnroll,
    /// Worklist was cut by the path-exploration budget.
    IncompleteBudget,
}

impl PathStatus {
    /// Whether this status leaves any doubt about safety of that path.
    pub fn is_unknown_like(self) -> bool {
        matches!(
            self,
            PathStatus::UnknownSolver | PathStatus::IncompleteUnroll | PathStatus::IncompleteBudget
        )
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct PathResult {
    pub index: u32,
    pub status: PathStatus,
    pub depth: u32,
    pub branches: Vec<PathBranch>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FailureFinding {
    pub kind: FailureKind,
    /// Stable node id of the failing assert / divisor expression.
    pub node_id: u32,
    pub line: u32,
    pub message: Option<String>,
    pub path_index: u32,
    /// Concrete input assignment extracted from the SMT model.
    pub counterexample: BTreeMap<String, u64>,
    pub path_branches: Vec<PathBranch>,
    /// Independent replay outcome through the concrete interpreter.
    pub replay: ReplayCheck,
    /// Path condition in native (solver-independent) form plus the SSA
    /// bindings needed to evaluate it. This lets an external party
    /// independently enumerate satisfying inputs without trusting Z3.
    pub native_path_condition: Vec<NBool>,
    pub native_ssa: SsaEnv,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// At least one replay-reproduced counterexample exists.
    Unsafe,
    /// Every reachable path was explored and held.
    Safe,
    /// No failure found, but some paths remain unexplored/uncertain.
    Unknown,
}

#[derive(Debug, Clone, Serialize)]
pub struct BudgetReport {
    pub paths_explored: u32,
    pub max_paths: u32,
    pub truncated: bool,
    pub truncation_reason: Option<String>,
    pub max_depth: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct PathCounts {
    pub safe: u32,
    pub failed: u32,
    pub infeasible: u32,
    pub unknown: u32,
    pub incomplete_unroll: u32,
    pub incomplete_budget: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct AnalysisReport {
    pub run_id: String,
    pub service_name: String,
    pub service_version: String,
    pub smt_backend: String,
    pub smt_version: String,
    pub verdict: Verdict,
    pub engine: EngineConfigEcho,
    pub budget: BudgetReport,
    pub path_counts: PathCounts,
    pub findings: Vec<FailureFinding>,
    pub paths: Vec<PathResult>,
}

#[derive(Debug, Clone, Serialize)]
pub struct EngineConfigEcho {
    pub max_paths: u32,
    pub loop_unroll: u32,
    pub solver_timeout_ms: u32,
}

impl EngineConfigEcho {
    pub fn from(cfg: &EngineConfig) -> Self {
        EngineConfigEcho {
            max_paths: cfg.max_paths,
            loop_unroll: cfg.loop_unroll,
            solver_timeout_ms: cfg.solver_timeout_ms,
        }
    }
}

/// Internal inconsistency: the solver reported a failure the independent
/// replay could not reproduce. Never exposed as "safe".
#[derive(Debug)]
pub struct ReplayMismatch {
    pub run_id: String,
    pub node_id: u32,
    pub detail: String,
}

impl std::fmt::Display for ReplayMismatch {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "run {}: internal inconsistency at node {}: {}",
            self.run_id, self.node_id, self.detail
        )
    }
}
impl std::error::Error for ReplayMismatch {}
