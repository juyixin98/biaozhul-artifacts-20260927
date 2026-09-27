//! Checks emitted during analysis and the resulting report shapes.
//!
//! Verdict vocabulary (important for not overstating results):
//! * [`CheckVerdict::Safe`] — no concrete execution can fail here;
//! * [`CheckVerdict::PossibleFailure`] — the abstract domain cannot rule a
//!   failure out; this is an over-approximation, NOT a claim the program is
//!   buggy;
//! * [`CheckVerdict::GuaranteedFailure`] — the abstract state itself proves
//!   that every reaching execution fails;
//! * [`CheckVerdict::Unreachable`] — the site cannot be reached.
use ia_intervals::Interval;
use ia_lang::span::Span;
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CheckKind {
    Overflow,
    DivByZero,
    IndexBounds,
    Assertion,
}

impl CheckKind {
    pub fn as_str(self) -> &'static str {
        match self {
            CheckKind::Overflow => "overflow",
            CheckKind::DivByZero => "div_by_zero",
            CheckKind::IndexBounds => "index_bounds",
            CheckKind::Assertion => "assertion",
        }
    }
}

/// What the abstract state proves about the *possibility* of failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureCertainty {
    /// Failure is possible on at least some inputs the state represents.
    Possible,
    /// Failure occurs for every input reaching the site.
    Guaranteed,
}

/// Final per-site verdict.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CheckVerdict {
    Safe,
    PossibleFailure,
    GuaranteedFailure,
    Unreachable,
}

impl CheckVerdict {
    pub fn as_str(self) -> &'static str {
        match self {
            CheckVerdict::Safe => "safe",
            CheckVerdict::PossibleFailure => "possible_failure",
            CheckVerdict::GuaranteedFailure => "guaranteed_failure",
            CheckVerdict::Unreachable => "unreachable",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CheckRecord {
    /// Stable id: `"{kind}:{start_offset}"`.
    pub id: String,
    pub kind: CheckKind,
    pub span: Span,
    /// Interval of the relevant abstract value (index for bounds checks,
    /// divisor for div-by-zero, full i64 for overflow) at the site.
    pub observed: Interval,
    /// Present only when failure is not ruled out.
    pub certainty: Option<FailureCertainty>,
    pub verdict: CheckVerdict,
    pub reachable: bool,
    pub explanation: String,
    /// Array length for index checks; otherwise `None`.
    pub array_len: Option<i64>,
}

/// One recorded point in the abstract traversal, making the derivation
/// inspectable.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TraceEvent {
    pub seq: u64,
    pub kind: String,
    pub span: Span,
    pub detail: String,
    pub vars: Vec<(String, Interval)>,
}

/// Summary counts used in the report header.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct VerdictCounts {
    pub safe: usize,
    pub possible_failure: usize,
    pub guaranteed_failure: usize,
    pub unreachable: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixpointStrategy {
    /// Plain Kleene iteration without widening (bounded for tiny ranges).
    Plain,
    /// Widening until stabilisation, then optional narrowing iterations.
    WidenNarrow,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FixpointStats {
    pub strategy: FixpointStrategy,
    pub iterations: usize,
    pub narrowing_iterations: usize,
    pub widened: bool,
    pub early_cutoff: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AnalysisReport {
    pub solver_version: String,
    pub lang_version: String,
    pub checks: Vec<CheckRecord>,
    pub counts: VerdictCounts,
    pub trace: Vec<TraceEvent>,
    pub fixpoints: Vec<(usize, FixpointStats)>,
}
