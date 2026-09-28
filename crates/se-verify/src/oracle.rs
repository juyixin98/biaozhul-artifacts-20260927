//! Small-domain exhaustive oracle.
//!
//! Independent ground truth: run the concrete interpreter on every input assignment in
//! the declared domains and collect outcomes. No SMT solver, no symbolic engine code
//! is involved, so agreement between this and the engine is meaningful evidence.

use std::collections::BTreeMap;

use se_lang::interp::{self, FailureKind, FlowOutcome, RunOpts};
use se_lang::Program;
use serde::Serialize;

#[derive(Clone, Debug, Serialize)]
pub struct OracleFailure {
    pub kind: String,
    pub stmt_id: usize,
    pub op: Option<String>,
    pub inputs: BTreeMap<String, u64>,
}

#[derive(Clone, Debug, Serialize)]
pub struct OracleSummary {
    /// Ground-truth verdict across the domain.
    pub verdict: String,
    pub total_assignments: u64,
    pub completed: u64,
    pub infeasible_assume: u64,
    pub failures: Vec<OracleFailure>,
    /// Distinct failing (kind, stmt_id) pairs in encounter order.
    pub failure_sites: Vec<(String, usize)>,
    pub truncated: bool,
}

/// Per-assignment result used by cross-checking tests.
#[derive(Clone, Debug)]
pub struct OracleResult {
    pub assignment_index: u64,
    pub inputs: BTreeMap<String, u64>,
    pub outcome: FlowOutcome,
    pub steps: u64,
}

/// Enumerate the domain concretely. `cap` bounds the number of assignments; when the
/// domain exceeds it, `truncated` is set and the verdict is `unknown`.
pub fn exhaustive_oracle(program: &Program, cap: u64, opts: RunOpts) -> OracleSummary {
    let mut completed = 0u64;
    let mut infeasible = 0u64;
    let mut failures: Vec<OracleFailure> = Vec::new();
    let mut sites: Vec<(String, usize)> = Vec::new();
    let mut total = 0u64;

    let truncated = !interp::enumerate_inputs(program, cap, |idx, assignment| {
        let r = interp::run(program, assignment, opts.clone());
        total = idx + 1;
        match r.outcome {
            FlowOutcome::Completed => completed += 1,
            FlowOutcome::InfeasibleAssume => infeasible += 1,
            FlowOutcome::Failed(f) => {
                let key = (f.kind.as_str().to_string(), f.stmt_id);
                if !sites.contains(&key) {
                    sites.push(key);
                }
                failures.push(OracleFailure {
                    kind: f.kind.as_str().to_string(),
                    stmt_id: f.stmt_id,
                    op: f.op.map(|s| s.to_string()),
                    inputs: assignment.clone(),
                });
            }
        }
    });

    let verdict = if truncated {
        "unknown"
    } else if !failures.is_empty() {
        "violation"
    } else {
        "holds"
    };

    OracleSummary {
        verdict: verdict.to_string(),
        total_assignments: total,
        completed,
        infeasible_assume: infeasible,
        failures,
        failure_sites: sites,
        truncated,
    }
}

/// Convenience: enumerate the domain and return all raw outcomes (also capped).
pub fn enumerate_outcomes(program: &Program, cap: u64, opts: RunOpts) -> (Vec<OracleResult>, bool) {
    let mut out = Vec::new();
    let truncated = !interp::enumerate_inputs(program, cap, |idx, assignment| {
        let r = interp::run(program, assignment, opts.clone());
        out.push(OracleResult {
            assignment_index: idx,
            inputs: assignment.clone(),
            outcome: r.outcome,
            steps: r.steps,
        });
    });
    (out, truncated)
}

/// Find the first concrete failing assignment for a given (kind, stmt_id) site, if any.
pub fn first_failure_for(
    program: &Program,
    cap: u64,
    kind: FailureKind,
    stmt_id: usize,
    opts: RunOpts,
) -> Option<BTreeMap<String, u64>> {
    let mut hit = None;
    interp::enumerate_inputs(program, cap, |_, assignment| {
        if hit.is_some() {
            return;
        }
        let r = interp::run(program, assignment, opts.clone());
        if let FlowOutcome::Failed(f) = r.outcome {
            if f.kind == kind && f.stmt_id == stmt_id {
                hit = Some(assignment.clone());
            }
        }
    });
    hit
}
