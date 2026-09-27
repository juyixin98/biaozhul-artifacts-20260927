//! HTTP request/response DTOs for the analysis service.
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct AnalyzeRequest {
    /// IAL source text.
    pub source: String,
    /// Caller-chosen correlation id; generated when absent.
    #[serde(default)]
    pub request_id: Option<String>,
    #[serde(default)]
    pub narrowing: Option<bool>,
    #[serde(default)]
    pub plain_fixpoint: Option<bool>,
    #[serde(default)]
    pub max_trace_events: Option<usize>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct VerifyRequest {
    pub source: String,
    #[serde(default)]
    pub request_id: Option<String>,
    #[serde(default)]
    pub enumeration_cap: Option<u64>,
    #[serde(default)]
    pub step_limit: Option<u64>,
    #[serde(default)]
    pub narrowing: Option<bool>,
}

/// A single diagnostic for parse/validation failures, rendered with its
/// source location.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Diagnostic {
    pub message: String,
    pub start_line: u32,
    pub start_column: u32,
    pub end_offset: usize,
    pub source_excerpt: String,
}

/// Processing stage recorded for the structured log/echo.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Step {
    pub stage: String,
    pub detail: String,
}

/// Common envelope. Failures (`diagnostics`) and uncertain findings are kept
/// in separate fields rather than mixed into a flat status string.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Envelope<T: Serialize> {
    pub request_id: String,
    pub service: &'static str,
    pub service_version: String,
    pub lang_version: String,
    pub solver_version: String,
    pub ok: bool,
    /// Hard failures: parse/validation/input errors. Empty when ok=true.
    pub diagnostics: Vec<Diagnostic>,
    pub steps: Vec<Step>,
    pub data: Option<T>,
}
