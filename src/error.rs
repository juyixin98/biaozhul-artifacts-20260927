//! Error taxonomy for the wavelet-matrix service.
//!
//! Every failure path is a distinct variant so that callers (and the HTTP
//! layer) can report a stable machine-readable `kind` instead of a string.

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WmError {
    /// Building an index over an empty sequence is rejected.
    EmptyInput,
    /// Window `[l, r)` does not fit inside the sequence of length `len`
    /// (`l > r` or `r > len`).
    InvalidRange {
        l: usize,
        r: usize,
        len: usize,
    },
    /// Window `[l, r)` is empty (`l == r`); queries over it are rejected.
    EmptyRange {
        l: usize,
        r: usize,
    },
    /// `k` must satisfy `0 <= k < r - l` (k is 0-based).
    KOutOfBounds {
        k: usize,
        window: usize,
    },
    /// Index names must match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`.
    InvalidIndexName(String),
    IndexNotFound(String),
    DuplicateIndex(String),
    /// On-disk payload failed a structural or checksum check.
    CorruptFormat(String),
    /// File was written by an incompatible format version.
    UnsupportedVersion(u32),
    Io(String),
    /// Malformed request (missing parameter, unknown op, ...).
    BadRequest(String),
}

impl WmError {
    /// Stable machine-readable category, surfaced in API responses and logs.
    pub fn kind(&self) -> &'static str {
        match self {
            WmError::EmptyInput => "empty_input",
            WmError::InvalidRange { .. } => "invalid_range",
            WmError::EmptyRange { .. } => "empty_range",
            WmError::KOutOfBounds { .. } => "k_out_of_bounds",
            WmError::InvalidIndexName(_) => "invalid_index_name",
            WmError::IndexNotFound(_) => "index_not_found",
            WmError::DuplicateIndex(_) => "duplicate_index",
            WmError::CorruptFormat(_) => "corrupt_format",
            WmError::UnsupportedVersion(_) => "unsupported_version",
            WmError::Io(_) => "io_error",
            WmError::BadRequest(_) => "bad_request",
        }
    }
}

impl fmt::Display for WmError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WmError::EmptyInput => write!(f, "cannot build an index over an empty sequence"),
            WmError::InvalidRange { l, r, len } => write!(
                f,
                "window [{l}, {r}) does not fit inside a sequence of length {len}"
            ),
            WmError::EmptyRange { l, r } => {
                write!(f, "window [{l}, {r}) is empty; queries require l < r")
            }
            WmError::KOutOfBounds { k, window } => write!(
                f,
                "k = {k} is out of bounds for a window of length {window} (k is 0-based)"
            ),
            WmError::InvalidIndexName(n) => write!(
                f,
                "invalid index name {n:?}; expected [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}"
            ),
            WmError::IndexNotFound(n) => write!(f, "index {n:?} not found"),
            WmError::DuplicateIndex(n) => write!(f, "index {n:?} already exists"),
            WmError::CorruptFormat(why) => write!(f, "corrupt index file: {why}"),
            WmError::UnsupportedVersion(v) => {
                write!(f, "unsupported format version {v}")
            }
            WmError::Io(msg) => write!(f, "I/O error: {msg}"),
            WmError::BadRequest(msg) => write!(f, "bad request: {msg}"),
        }
    }
}

impl std::error::Error for WmError {}

impl From<std::io::Error> for WmError {
    fn from(e: std::io::Error) -> Self {
        WmError::Io(e.to_string())
    }
}
