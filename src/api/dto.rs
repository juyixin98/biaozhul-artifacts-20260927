//! HTTP transport DTOs. These are deliberately separate from the core domain types:
//! the API can evolve (defaults, aliases, envelope fields) without changing the
//! extraction algorithm.

use serde::{Deserialize, Serialize};

use crate::core::{ExtractionReport, OrderPolicy};
use crate::language::Formula;

/// Request body for both `/extract` (sync) and `/jobs` (create async job).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExtractRequest {
    #[serde(flatten)]
    pub formula: Formula,
    /// Named solver; falls back to the server default when omitted.
    #[serde(default)]
    pub solver: Option<String>,
    /// Independent verifier solver; `false` disables certification.
    #[serde(default)]
    pub verify: Option<bool>,
    #[serde(default)]
    pub order: Option<OrderPolicy>,
    /// Maximum total solver calls including the initial satisfiability check.
    #[serde(default)]
    pub max_solver_calls: Option<usize>,
    /// Per-call branching-decision budget for in-process solvers.
    #[serde(default)]
    pub max_decisions: Option<u64>,
}

/// Stable error envelope. `request_id` is echoed so clients can quote it in bug
/// reports; `category` is machine-parseable; `details` never contains clause
/// literals from sensitive constraints.
#[derive(Debug, Clone, Serialize)]
pub struct ApiError {
    pub error: ErrorBody,
    pub request_id: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ErrorBody {
    pub category: ErrorCategory,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCategory {
    MalformedJson,
    InvalidInput,
    UnknownSolver,
    JobNotFound,
    JobNotCancellable,
    MethodNotAllowed,
    Internal,
}

#[derive(Debug, Clone, Serialize)]
pub struct CreatedJob {
    pub job_id: String,
    pub status: &'static str,
}

/// Job envelope returned by GET /jobs/:id.
#[derive(Debug, Clone, Serialize)]
pub struct JobEnvelope {
    pub job_id: String,
    pub lifecycle: JobLifecycle,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub report: Option<ExtractionReport>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum JobLifecycle {
    Queued,
    Running,
    Done,
    Cancelled,
}

#[derive(Debug, Serialize)]
pub struct Health {
    pub status: &'static str,
    pub default_solver: String,
    pub verifier_solver: String,
    pub available_solvers: Vec<String>,
}

/// Default OrderPolicy used when the request omits one.
pub fn order_or_default(order: Option<OrderPolicy>) -> OrderPolicy {
    order.unwrap_or_default()
}
