//! HTTP backend (Axum).
//!
//! Thin transport layer over the real modules:
//! [`crate::language`] parses input, [`crate::solver`] decides,
//! [`crate::extract`] finds cores, [`crate::verify`] certifies them independently.
//!
//! Endpoints:
//! * `GET  /health`
//! * `GET  /v1/solvers`
//! * `POST /v1/extract`            — synchronous extraction
//! * `POST /v1/jobs`               — enqueue asynchronous extraction
//! * `GET  /v1/jobs/:id`           — job status/result
//! * `POST /v1/jobs/:id/cancel`    — cooperative cancellation

pub mod handlers;

use crate::config::Config;
use crate::diagnostics::{Redactor, RequestId};
use crate::solver::Solver;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tokio::sync::Semaphore;

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub cfg: Arc<Config>,
    /// Primary solver used for extraction.
    pub primary: Arc<dyn Solver>,
    /// Independent backend used for boundary verification (distinct implementation).
    pub oracle: Arc<dyn Solver>,
    pub jobs: Arc<Mutex<HashMap<String, Job>>>,
    /// Cancellation token per live job.
    pub cancels: Arc<Mutex<HashMap<String, crate::solver::CancelToken>>>,
    pub redactor: Redactor,
    /// Bounds the number of extraction threads running at once.
    pub concurrency: Arc<Semaphore>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobCore {
    pub member_ids: Vec<String>,
    pub size: usize,
    pub verdict: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Job {
    pub id: String,
    pub request_id: String,
    pub status: JobStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<ExtractResponse>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ApiError>,
    pub created_at_ms: u128,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
}

/// JSON-encoded error body. Carries the correlation id and a stable error code.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiError {
    pub error: ErrorBody,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErrorBody {
    pub code: String,
    pub message: String,
    pub request_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Request / response DTOs
// ---------------------------------------------------------------------------

/// One constraint in JSON form: `{"id": "a", "literals": [1, -2]}`.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ConstraintDto {
    pub id: String,
    pub literals: Vec<i32>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct ExtractRequest {
    /// Variable universe; inferred when omitted.
    #[serde(default)]
    pub nvars: Option<usize>,
    /// Structured constraints (preferred).
    #[serde(default)]
    pub constraints: Option<Vec<ConstraintDto>>,
    /// Or raw text (local id format / DIMACS). Exactly one of `constraints`/`text`.
    #[serde(default)]
    pub text: Option<String>,
    /// `"find_one"` (default) or `"find_all"`.
    #[serde(default)]
    pub mode: Option<String>,
    /// Solver-call decision budget; 0/absent = unlimited (subject to server cap).
    #[serde(default)]
    pub budget: Option<u64>,
    /// Verify the report with the independent oracle on the API boundary.
    #[serde(default)]
    pub verify: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtractResponse {
    pub request_id: String,
    pub termination: String,
    pub input_satisfiable: bool,
    pub cores: Vec<CoreOut>,
    pub retained_candidate: Vec<String>,
    pub untested: Vec<String>,
    pub trace: Vec<serde_json::Value>,
    pub solver: String,
    pub budget_limit: u64,
    pub budget_used: u64,
    pub rounds: usize,
    pub note: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub verification: Option<crate::verify::VerificationReport>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CoreOut {
    pub member_ids: Vec<String>,
    pub size: usize,
    /// `certified_mus` or `uncertified_unsat_candidate`.
    pub verdict: String,
    /// Minimality witnesses are returned only when the client asks for them,
    /// because they can be large.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub minimality_witnesses: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct JobCreateRequest {
    #[serde(flatten)]
    pub extract: ExtractRequest,
    #[serde(default)]
    pub include_witnesses: Option<bool>,
}

#[derive(Debug, Clone, Serialize)]
pub struct JobCreated {
    pub job_id: String,
    pub request_id: String,
    pub status: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct JobCancelled {
    pub job_id: String,
    pub status: String,
    pub note: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SolverInfo {
    pub role: String,
    pub name: String,
}

/// Validation failures mapped to specific 4xx codes.
#[derive(Debug)]
pub struct RequestValidation {
    pub code: &'static str,
    pub message: String,
}

pub(crate) fn rid_from_header(headers: &axum::http::HeaderMap) -> RequestId {
    match headers
        .get("x-request-id")
        .and_then(|h| h.to_str().ok())
        .map(str::to_string)
    {
        Some(id) if !id.is_empty() && id.len() <= 128 => RequestId(id),
        _ => RequestId::new(),
    }
}

pub(crate) fn error_json(code: &str, message: impl Into<String>, rid: &RequestId) -> ApiError {
    ApiError {
        error: ErrorBody {
            code: code.to_string(),
            message: message.into(),
            request_id: rid.to_string(),
            details: None,
        },
    }
}
