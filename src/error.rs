//! Error taxonomy. Every failure the system can surface is classified into one
//! of a closed set of categories so callers (and tests) can assert *why* a
//! request was rejected or could not be decided, rather than only that it
//! failed.

use std::io;

/// Result alias used across the crate.
pub type Result<T> = std::result::Result<T, MphfError>;

/// Coarse failure category, independent of the natural-language message.
///
/// Wire names are stable (they appear in the file format / HTTP API), so they
/// are `snake_case` constants rather than derived debug names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// A configuration / argument value was invalid.
    InvalidConfig,
    /// Input keyset violated a precondition (e.g. load factor out of range).
    InvalidInput,
    /// Peeling could not remove all hyperedges for every seed attempted.
    PeelingFailed,
    /// The on-disk index did not match the expected format magic bytes.
    FormatMagic,
    /// The on-disk index used an unsupported format/algorithm version.
    FormatVersion,
    /// A stored field was inconsistent (lengths, offsets, mode, ...).
    FormatCorrupt,
    /// The payload checksum did not verify.
    ChecksumMismatch,
    /// The referenced set does not exist on this server / path.
    SetNotFound,
    /// The request was malformed (HTTP layer).
    BadRequest,
    /// Something went wrong internally; the result is undetermined.
    Internal,
}

impl ErrorKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::InvalidConfig => "invalid_config",
            ErrorKind::InvalidInput => "invalid_input",
            ErrorKind::PeelingFailed => "peeling_failed",
            ErrorKind::FormatMagic => "format_magic",
            ErrorKind::FormatVersion => "format_version",
            ErrorKind::FormatCorrupt => "format_corrupt",
            ErrorKind::ChecksumMismatch => "checksum_mismatch",
            ErrorKind::SetNotFound => "set_not_found",
            ErrorKind::BadRequest => "bad_request",
            ErrorKind::Internal => "internal",
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum MphfError {
    #[error("{message}")]
    Config { kind: ErrorKind, message: String },
    #[error("peeling failed: {message}")]
    Peel { message: String },
    #[error("index format error ({kind:?}): {message}")]
    Format { kind: ErrorKind, message: String },
    #[error("set not found: {0}")]
    SetNotFound(String),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error("UTF-8 error: {0}")]
    Utf8(#[from] std::string::FromUtf8Error),
    #[error("internal error: {0}")]
    Internal(String),
}

impl MphfError {
    pub fn config(message: impl Into<String>) -> Self {
        MphfError::Config {
            kind: ErrorKind::InvalidConfig,
            message: message.into(),
        }
    }

    pub fn invalid_input(message: impl Into<String>) -> Self {
        MphfError::Config {
            kind: ErrorKind::InvalidInput,
            message: message.into(),
        }
    }

    pub fn format(kind: ErrorKind, message: impl Into<String>) -> Self {
        MphfError::Format {
            kind,
            message: message.into(),
        }
    }

    pub fn kind(&self) -> ErrorKind {
        match self {
            MphfError::Config { kind, .. } => *kind,
            MphfError::Peel { .. } => ErrorKind::PeelingFailed,
            MphfError::Format { kind, .. } => *kind,
            MphfError::SetNotFound(_) => ErrorKind::SetNotFound,
            MphfError::Io(_) | MphfError::Utf8(_) => ErrorKind::Internal,
            MphfError::Internal(_) => ErrorKind::Internal,
        }
    }
}
