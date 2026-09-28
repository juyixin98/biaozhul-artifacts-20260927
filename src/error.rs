//! Error taxonomy shared across the kernel, format codec, persistence adapter and HTTP layer.
//!
//! Every fallible operation in this crate returns [`CodecError`]. The [`ErrorCategory`]
//! discriminant is the *data and error contract* between modules and with HTTP clients:
//!
//! | category             | meaning                                                        | HTTP |
//! |----------------------|----------------------------------------------------------------|------|
//! | [`ErrorCategory::Input`]            | malformed wire data or invalid caller argument      | 400  |
//! | [`ErrorCategory::StateConflict`]    | dependency missing, digest mismatch, stale metadata | 409  |
//! | [`ErrorCategory::ResourceExhausted`] | declared/actual output exceeds safety limits       | 413  |
//! | [`ErrorCategory::NotFound`]         | block id does not exist                              | 404  |
//! | [`ErrorCategory::ComputeFailure`]   | internal invariant broken (bug, never expected)     | 500  |
//!
//! A storage I/O failure is reported with the underlying OS message as
//! [`ErrorCategory::ComputeFailure`] because it is an environment failure rather than bad input.

use std::fmt;

/// Broad failure class. Stable string representation via [`AsRef<str>`] is part of
/// the contract and is emitted in JSON error bodies and test logs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorCategory {
    /// Malformed encoding, bad argument, CRC failure.
    Input,
    /// Missing previous block, dictionary digest mismatch, duplicate id.
    StateConflict,
    /// Output ratio / total byte / chain-depth limit exceeded.
    ResourceExhausted,
    /// Referenced block does not exist.
    NotFound,
    /// Internal invariant violation or host I/O failure.
    ComputeFailure,
}

impl AsRef<str> for ErrorCategory {
    fn as_ref(&self) -> &'static str {
        match self {
            ErrorCategory::Input => "input_error",
            ErrorCategory::StateConflict => "state_conflict",
            ErrorCategory::ResourceExhausted => "resource_exhausted",
            ErrorCategory::NotFound => "not_found",
            ErrorCategory::ComputeFailure => "compute_failure",
        }
    }
}

impl fmt::Display for ErrorCategory {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_ref())
    }
}

/// Crate-wide error type. `detail` always carries a specific, assertion-grade reason
/// (e.g. `"match distance 4097 exceeds window 4096"`), never a generic message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodecError {
    pub category: ErrorCategory,
    pub detail: String,
}

impl CodecError {
    pub fn new(category: ErrorCategory, detail: impl Into<String>) -> Self {
        Self {
            category,
            detail: detail.into(),
        }
    }

    pub fn input(detail: impl Into<String>) -> Self {
        Self::new(ErrorCategory::Input, detail)
    }

    pub fn state(detail: impl Into<String>) -> Self {
        Self::new(ErrorCategory::StateConflict, detail)
    }

    pub fn exhausted(detail: impl Into<String>) -> Self {
        Self::new(ErrorCategory::ResourceExhausted, detail)
    }

    pub fn not_found(detail: impl Into<String>) -> Self {
        Self::new(ErrorCategory::NotFound, detail)
    }

    pub fn internal(detail: impl Into<String>) -> Self {
        Self::new(ErrorCategory::ComputeFailure, detail)
    }
}

impl fmt::Display for CodecError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.category.as_ref(), self.detail)
    }
}

impl std::error::Error for CodecError {}

pub type Result<T> = std::result::Result<T, CodecError>;
