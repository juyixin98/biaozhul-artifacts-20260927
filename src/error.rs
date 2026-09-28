//! Error contract shared by every layer (index core, persistence, service).
//!
//! Every fallible operation returns [`Error`], whose [`Error::category`] is
//! one of four mutually exclusive classes required by the service contract:
//!
//! * [`ErrorCategory::InvalidInput`]   — caller-supplied data is malformed
//! * [`ErrorCategory::StateConflict`]  — request conflicts with current state
//! * [`ErrorCategory::ResourceExhausted`] — configured limits / memory reached
//! * [`ErrorCategory::ComputeFailure`] — internal invariant or I/O failure
//!
//! Persistence corruption is a *compute* failure from the caller's point of
//! view (the caller cannot fix the request), but it carries the stable code
//! `"persistence_corrupt"` so tests can assert the precise failure reason.

use std::io;

/// Coarse failure class, stable across versions. Used verbatim in JSON errors.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorCategory {
    InvalidInput,
    StateConflict,
    ResourceExhausted,
    ComputeFailure,
}

impl ErrorCategory {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCategory::InvalidInput => "invalid_input",
            ErrorCategory::StateConflict => "state_conflict",
            ErrorCategory::ResourceExhausted => "resource_exhausted",
            ErrorCategory::ComputeFailure => "compute_failure",
        }
    }
}

/// Stable machine-readable codes (the `code` field of error responses).
pub mod codes {
    pub const EMPTY_TEXT: &str = "empty_text";
    pub const TEXT_TOO_LARGE: &str = "text_too_large";
    pub const BAD_SAMPLE_INTERVAL: &str = "bad_sample_interval";
    pub const BAD_BASE64: &str = "bad_base64";
    pub const BAD_ENCODING: &str = "bad_encoding";
    pub const BAD_NAME: &str = "bad_name";
    pub const MISSING_FIELD: &str = "missing_field";
    pub const NOT_FOUND: &str = "index_not_found";
    pub const ALREADY_EXISTS: &str = "index_already_exists";
    pub const CATALOG_CORRUPT: &str = "catalog_corrupt";
    pub const PERSISTENCE_CORRUPT: &str = "persistence_corrupt";
    pub const IO: &str = "io_error";
    pub const INVARIANT: &str = "internal_invariant";
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    // ---- invalid input ----
    #[error("text must contain at least one byte")]
    EmptyText,
    #[error("text of {size} bytes exceeds the configured limit of {limit} bytes")]
    TextTooLarge { size: usize, limit: usize },
    #[error("sample interval must be in 1..=65535, got {got}")]
    BadSampleInterval { got: u32 },
    #[error("invalid base64 payload: {0}")]
    BadBase64(String),
    #[error("invalid index name {name:?}: {reason}")]
    BadName { name: String, reason: &'static str },
    #[error("missing required field: {0}")]
    MissingField(&'static str),
    #[error("text field must be base64 (b64) or hex (hex), got {0:?}")]
    BadEncoding(String),

    // ---- state conflict ----
    #[error("index {0:?} does not exist")]
    NotFound(String),
    #[error("index {0:?} already exists")]
    AlreadyExists(String),

    // ---- persistence / compute ----
    #[error("catalog file is corrupted: {0}")]
    CatalogCorrupt(String),
    #[error("persisted index is corrupted ({section}): {detail}")]
    PersistenceCorrupt { section: String, detail: String },
    #[error("internal invariant violated: {0}")]
    Invariant(String),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
}

impl Error {
    pub fn category(&self) -> ErrorCategory {
        match self {
            Error::EmptyText
            | Error::BadSampleInterval { .. }
            | Error::BadBase64(_)
            | Error::BadName { .. }
            | Error::MissingField(_)
            | Error::BadEncoding(_) => ErrorCategory::InvalidInput,
            // Oversized uploads are a resource-limit problem (HTTP 413).
            Error::TextTooLarge { .. } => ErrorCategory::ResourceExhausted,
            Error::NotFound(_) | Error::AlreadyExists(_) => ErrorCategory::StateConflict,
            Error::CatalogCorrupt(_)
            | Error::PersistenceCorrupt { .. }
            | Error::Invariant(_)
            | Error::Io(_) => ErrorCategory::ComputeFailure,
        }
    }

    /// HTTP status mapped at the service boundary.
    pub fn http_status(&self) -> u16 {
        match self {
            Error::EmptyText
            | Error::BadSampleInterval { .. }
            | Error::BadBase64(_)
            | Error::BadName { .. }
            | Error::MissingField(_)
            | Error::BadEncoding(_) => 400,
            Error::TextTooLarge { .. } => 413,
            Error::NotFound(_) => 404,
            Error::AlreadyExists(_) => 409,
            Error::CatalogCorrupt(_)
            | Error::PersistenceCorrupt { .. }
            | Error::Invariant(_)
            | Error::Io(_) => 500,
        }
    }

    pub fn code(&self) -> &'static str {
        match self {
            Error::EmptyText => codes::EMPTY_TEXT,
            Error::TextTooLarge { .. } => codes::TEXT_TOO_LARGE,
            Error::BadSampleInterval { .. } => codes::BAD_SAMPLE_INTERVAL,
            Error::BadBase64(_) => codes::BAD_BASE64,
            Error::BadName { .. } => codes::BAD_NAME,
            Error::MissingField(_) => codes::MISSING_FIELD,
            Error::BadEncoding(_) => codes::BAD_ENCODING,
            Error::NotFound(_) => codes::NOT_FOUND,
            Error::AlreadyExists(_) => codes::ALREADY_EXISTS,
            Error::CatalogCorrupt(_) => codes::CATALOG_CORRUPT,
            Error::PersistenceCorrupt { .. } => codes::PERSISTENCE_CORRUPT,
            Error::Invariant(_) => codes::INVARIANT,
            Error::Io(_) => codes::IO,
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;
