//! Error contract shared across the input language, solver kernel and HTTP API.
//!
//! Four categories are kept distinct end to end so callers can tell a malformed
//! request (fix the payload) apart from state conflicts (fix the model),
//! resource exhaustion (verdict unknown, raise limits or shrink the model) and
//! internal computation failures (bug).

use axum::http::StatusCode;
use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    /// Syntactically / structurally invalid input: bad JSON, empty name,
    /// transition uses a label outside the declared alphabet, ...
    InputError,
    /// Well-formed input that contradicts itself: unknown state reference,
    /// initial state not declared, silent action also listed as observable, ...
    StateConflict,
    /// Search bound or input-size limit hit. The inclusion question is
    /// *unanswered* (`unknown`), not false.
    ResourceExhausted,
    /// Something that should never happen (invariant violation).
    ComputationFailed,
}

impl ErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::InputError => "input_error",
            ErrorKind::StateConflict => "state_conflict",
            ErrorKind::ResourceExhausted => "resource_exhausted",
            ErrorKind::ComputationFailed => "computation_failed",
        }
    }

    pub fn http_status(self) -> StatusCode {
        match self {
            ErrorKind::InputError => StatusCode::BAD_REQUEST,
            // 409 Conflict: structurally valid request, model state disagrees.
            ErrorKind::StateConflict => StatusCode::CONFLICT,
            // 413 Payload Too Large: input refused up front on size grounds,
            // or search budget exhausted (same category, see CheckOutcome).
            ErrorKind::ResourceExhausted => StatusCode::PAYLOAD_TOO_LARGE,
            ErrorKind::ComputationFailed => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct EngineError {
    pub kind: ErrorKind,
    pub code: String,
    pub message: String,
}

impl EngineError {
    pub fn new(kind: ErrorKind, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            kind,
            code: code.into(),
            message: message.into(),
        }
    }

    pub fn input(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::InputError, code, message)
    }

    pub fn conflict(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::StateConflict, code, message)
    }

    pub fn exhausted(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::ResourceExhausted, code, message)
    }

    pub fn internal(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::new(ErrorKind::ComputationFailed, code, message)
    }
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}: {}", self.kind.as_str(), self.code, self.message)
    }
}

impl std::error::Error for EngineError {}

pub type EngineResult<T> = Result<T, EngineError>;
