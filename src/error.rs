//! Error contract shared by every layer of the service.
//!
//! Every failure the system can produce is assigned exactly one [`ErrorKind`].
//! The HTTP layer maps these to status codes (see [`ErrorKind::http_status`]),
//! the batch processor tags each item result with the same kind, and the test
//! logs record it verbatim. This keeps "bad input", "state conflict",
//! "resource exhausted" and "computation failed" distinguishable end to end.

use serde::{Deserialize, Serialize};

/// The four failure classes required by the specification, plus a fifth
/// `NotFound` class for unknown constraint ids on read-style operations.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// Malformed request: bad JSON, unknown variable, malformed constraint
    /// text, bad identifier, etc. The caller can fix the request and retry.
    Input,
    /// The request was well formed but conflicts with current service state:
    /// duplicate id on add, unknown id on update/delete/solve, or a batch
    /// operation aborted because an earlier item in the same batch failed.
    StateConflict,
    /// A declared resource limit was exceeded (variables, constraints, body
    /// size). Retrying unchanged cannot succeed; the request must shrink.
    ResourceExhausted,
    /// The arithmetic/graph computation failed for a well-formed request:
    /// integer overflow during relaxation, or an internal invariant broken
    /// (e.g. a malformed predecessor chain).
    ComputationFailed,
    /// No such resource (unknown id on a read endpoint, unknown route).
    NotFound,
}

impl ErrorKind {
    /// Stable HTTP status mapping. Documented in README §Error semantics.
    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::Input => 400,
            ErrorKind::StateConflict => 409,
            ErrorKind::ResourceExhausted => 413,
            ErrorKind::ComputationFailed => 422,
            ErrorKind::NotFound => 404,
        }
    }

    /// Short stable identifier written into logs and JSON responses.
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::Input => "input",
            ErrorKind::StateConflict => "state_conflict",
            ErrorKind::ResourceExhausted => "resource_exhausted",
            ErrorKind::ComputationFailed => "computation_failed",
            ErrorKind::NotFound => "not_found",
        }
    }
}

/// Structured error carrying a machine-readable kind and a human-readable
/// message plus optional machine-readable detail.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceError {
    pub kind: ErrorKind,
    /// Human-readable explanation, in English, no internal addresses.
    pub message: String,
    /// Optional machine-readable detail object (line/column, offending id,
    /// limit name, etc.).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<serde_json::Value>,
}

impl ServiceError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            detail: None,
        }
    }

    pub fn with_detail(mut self, detail: serde_json::Value) -> Self {
        self.detail = Some(detail);
        self
    }

    pub fn input(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Input, message)
    }
    pub fn state_conflict(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::StateConflict, message)
    }
    pub fn resource_exhausted(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::ResourceExhausted, message)
    }
    pub fn computation_failed(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::ComputationFailed, message)
    }
    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::NotFound, message)
    }
}

impl std::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.kind.as_str(), self.message)
    }
}

impl std::error::Error for ServiceError {}

/// Result alias used throughout the crate.
pub type ServiceResult<T> = Result<T, ServiceError>;
