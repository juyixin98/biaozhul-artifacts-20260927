//! Serializable analysis contract shared by the solver, the evidence checker
//! and the HTTP layer. Reports deliberately separate *definite* violations
//! from *possible* ones: an interval that only partly escapes a bound is
//! over-approximation uncertainty, never a proven error.

use crate::config::Config;
use crate::kernel::interval::Interval;
use crate::kernel::state::AbsState;
use crate::lang::Span;
use serde::{Deserialize, Serialize};

pub const ANALYZER_VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    /// Property provably holds on all reachable executions.
    Safe,
    /// Boundary may be crossed: soundness warning, not a proven failure.
    MaybeViolated,
    /// Every abstract value violates the property: a definite failure on
    /// every execution reaching this point.
    Violated,
    /// No abstract state reaches this statement.
    Unreachable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CheckKind {
    ArrayIndex,
    Overflow,
    Assert,
    UnreachableStmt,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Evidence {
    ArrayIndex {
        index: Interval,
        valid_lo: i64,
        valid_hi: i64,
        array_len: i64,
    },
    Overflow {
        /// Mathematical (unbounded) result interval before clamping.
        result: Interval,
        /// i64 value domain boundaries (always exactly the i64 range).
        i64_lo: i64,
        i64_hi: i64,
    },
    Assert {
        true_feasible: bool,
        false_feasible: bool,
    },
    Unreachable,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CheckRecord {
    pub id: usize,
    pub kind: CheckKind,
    pub span: Span,
    pub verdict: Verdict,
    /// Human-readable explanation of the verdict.
    pub detail: String,
    /// Structured evidence from which `verdict` is mechanically re-derivable.
    pub evidence: Evidence,
}

/// Verdict is a pure function of the evidence; the evidence checker recomputes
/// it and compares, so a report cannot just label a check however it likes.
pub fn verdict_of(kind: CheckKind, evidence: &Evidence) -> Verdict {
    if matches!(kind, CheckKind::UnreachableStmt) {
        return Verdict::Unreachable;
    }
    match evidence {
        Evidence::ArrayIndex {
            index,
            valid_lo,
            valid_hi,
            ..
        } => classify_vs_range(*index, i128::from(*valid_lo), i128::from(*valid_hi)),
        Evidence::Overflow {
            result,
            i64_lo,
            i64_hi,
        } => classify_vs_range(*result, i128::from(*i64_lo), i128::from(*i64_hi)),
        Evidence::Assert {
            true_feasible,
            false_feasible,
        } => match (*true_feasible, *false_feasible) {
            (true, false) => Verdict::Safe,
            (false, true) => Verdict::Violated,
            (true, true) => Verdict::MaybeViolated,
            (false, false) => Verdict::Unreachable,
        },
        Evidence::Unreachable => Verdict::Unreachable,
    }
}

/// Classify an interval against the inclusive bound [lo, hi].
pub fn classify_vs_range(i: Interval, lo: i128, hi: i128) -> Verdict {
    let bound = Interval::finite(lo, hi);
    match i.meet(bound) {
        Interval::Bottom => Verdict::Violated,
        _ if i.subset_of(&bound) => Verdict::Safe,
        _ => Verdict::MaybeViolated,
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum TraceEvent {
    Iteration {
        span: Span,
        iteration: usize,
        head_state: AbsState,
    },
    Widening {
        span: Span,
        iteration: usize,
    },
    Converged {
        span: Span,
        iterations: usize,
    },
    NarrowingPass {
        span: Span,
        pass: usize,
    },
    FixpointStabilized {
        span: Span,
        iterations: usize,
        narrowing_passes: usize,
    },
    FixpointIterationCap {
        span: Span,
        max_iterations: usize,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Observation {
    /// Interval of every value this assignment produced (joined across loops).
    AssignValue {
        span: Span,
        target: String,
        interval: Interval,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LoopInvariant {
    pub span: Span,
    /// State immediately before the loop (used to verify post-fixpoints).
    pub pre_state: AbsState,
    /// Abstract state at the loop head after widening/narrowing.
    pub invariant: AbsState,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Summary {
    pub total_checks: usize,
    pub safe: usize,
    pub possible_violations: usize,
    pub definite_violations: usize,
    pub unreachable: usize,
    #[serde(default)]
    pub definite_violation_ids: Vec<usize>,
    #[serde(default)]
    pub possible_violation_ids: Vec<usize>,
    #[serde(default)]
    pub unreachable_ids: Vec<usize>,
}

impl Verdict {
    pub fn as_str(&self) -> &'static str {
        match self {
            Verdict::Safe => "safe",
            Verdict::MaybeViolated => "maybe_violated",
            Verdict::Violated => "violated",
            Verdict::Unreachable => "unreachable",
        }
    }
}

impl std::fmt::Display for Verdict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnalysisReport {
    pub analyzer_version: String,
    pub program_hash: String,
    pub config: Config,
    pub checks: Vec<CheckRecord>,
    pub observations: Vec<Observation>,
    pub loop_invariants: Vec<LoopInvariant>,
    pub exit_state: AbsState,
    pub trace: Vec<TraceEvent>,
    pub summary: Summary,
}

impl AnalysisReport {
    pub fn summarize(&mut self) {
        let mut s = Summary {
            total_checks: self.checks.len(),
            safe: 0,
            possible_violations: 0,
            definite_violations: 0,
            unreachable: 0,
            definite_violation_ids: Vec::new(),
            possible_violation_ids: Vec::new(),
            unreachable_ids: Vec::new(),
        };
        for c in &self.checks {
            match c.verdict {
                Verdict::Safe => s.safe += 1,
                Verdict::MaybeViolated => {
                    s.possible_violations += 1;
                    s.possible_violation_ids.push(c.id);
                }
                Verdict::Violated => {
                    s.definite_violations += 1;
                    s.definite_violation_ids.push(c.id);
                }
                Verdict::Unreachable => {
                    s.unreachable += 1;
                    s.unreachable_ids.push(c.id);
                }
            }
        }
        self.summary = s;
    }
}
