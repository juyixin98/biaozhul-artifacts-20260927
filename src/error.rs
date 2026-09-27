//! Error contract shared by every module.
//!
//! Failures fall into exactly four classes.  The HTTP layer maps each class
//! to a distinct status code and every response body carries the class plus a
//! machine-readable `reason`, so test logs can distinguish input errors,
//! state conflicts, resource exhaustion and computation failures.

use serde::Serialize;

/// The four error classes required by the spec.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// Malformed rule set, step or request: invalid JSON, bad constants,
    /// unknown rule/field references, predicate that cannot evaluate.
    InputError,
    /// Step ordering violation, monitor already closed, rule-set version /
    /// content mismatch when restoring state, unknown monitor id.
    StateConflict,
    /// A configured resource bound was hit (steps, obligations, monitors).
    ResourceExhausted,
    /// Arithmetic overflow, non-numeric operand in a numeric predicate and
    /// other failures that are nobody's fault but cannot be completed.
    ComputationFailed,
}

impl ErrorKind {
    /// HTTP status used for the class.
    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::InputError => 400,
            ErrorKind::StateConflict => 409,
            ErrorKind::ResourceExhausted => 422,
            ErrorKind::ComputationFailed => 422,
        }
    }

    /// Short stable identifier written into logs (e.g. `input_error`).
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::InputError => "input_error",
            ErrorKind::StateConflict => "state_conflict",
            ErrorKind::ResourceExhausted => "resource_exhausted",
            ErrorKind::ComputationFailed => "computation_failed",
        }
    }
}

/// Structured application error.
#[derive(Debug, Clone)]
pub struct AppError {
    pub kind: ErrorKind,
    pub reason: &'static str,
    pub detail: String,
}

impl AppError {
    pub fn input(reason: &'static str, detail: impl Into<String>) -> Self {
        Self { kind: ErrorKind::InputError, reason, detail: detail.into() }
    }
    pub fn conflict(reason: &'static str, detail: impl Into<String>) -> Self {
        Self { kind: ErrorKind::StateConflict, reason, detail: detail.into() }
    }
    pub fn exhausted(reason: &'static str, detail: impl Into<String>) -> Self {
        Self { kind: ErrorKind::ResourceExhausted, reason, detail: detail.into() }
    }
    pub fn compute(reason: &'static str, detail: impl Into<String>) -> Self {
        Self { kind: ErrorKind::ComputationFailed, reason, detail: detail.into() }
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} [{}]: {}", self.kind.as_str(), self.reason, self.detail)
    }
}

impl std::error::Error for AppError {}

pub type AppResult<T> = Result<T, AppError>;
