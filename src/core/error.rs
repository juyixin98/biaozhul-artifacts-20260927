//! Error contract shared by the format kernel, codec, store and HTTP layers.
//!
//! Every failure that crosses a module boundary is one of four deliberately
//! distinguishable categories. Tests assert on the *category*, not just on
//! "an error happened":
//!
//! * [`Error::Input`]      — malformed/untrusted data. Caller's fault, never retried blindly.
//! * [`Error::State`]      — a sequencing/precondition conflict (missing predecessor, etc.).
//! * [`Error::Resource`]   — an enforced limit was hit before any unbounded allocation.
//! * [`Error::Compute`]    — the local environment failed (I/O, ...), with a source label.

use std::fmt;

/// Where a [`Error::Compute`] originated. Kept as data instead of a `std::error`
/// source so the value stays `Clone + PartialEq` and tests can assert on it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// Filesystem persistence layer.
    Store,
    /// Block file read/write helper.
    BlockFile,
    /// In-memory index bookkeeping.
    Index,
}

impl fmt::Display for Source {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Source::Store => "store",
            Source::BlockFile => "block-file",
            Source::Index => "index",
        })
    }
}

/// Structured error code. Stable strings; safe to surface over HTTP.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Code {
    // Input (malformed data)
    /// Magic bytes missing/wrong.
    BadMagic,
    /// Unsupported format version.
    BadVersion,
    /// Unknown frame type byte.
    BadFrameType,
    /// LEB128 varint truncated, overlong, or exceeds u64.
    BadVarint,
    /// A length field is zero or above the hard cap.
    BadLength,
    /// A match distance is zero or larger than bytes currently available.
    BadDistance,
    /// A match length is outside `[MIN_MATCH, MAX_MATCH]`.
    BadMatchLength,
    /// Token stream ended early or has trailing bytes.
    BadTokenStream,
    /// Payload CRC32 does not match the header.
    CrcMismatch,
    /// Declared decompressed length differs from the bytes actually produced.
    LengthMismatch,
    /// A raw UTF-8/string field is invalid (stream id, ...).
    BadString,
    /// HTTP request body is malformed.
    BadRequest,

    // State (sequencing conflicts)
    /// A dependent block was decoded before its predecessor.
    MissingPredecessor,
    /// The chain digest does not match the actual preceding dictionary.
    DigestMismatch,
    /// Block index does not continue the stream (`expected` is stored in `detail`).
    IndexGap,
    /// A block/file already exists where a write was requested.
    AlreadyExists,
    /// The referenced stream/block does not exist.
    NotFound,

    // Resource (enforced limits)
    /// Decompressed output would exceed the per-block output cap.
    OutputCapExceeded,
    /// Compressed payload exceeds the per-block payload cap.
    PayloadCapExceeded,
    /// Decompressed/compressed ratio would exceed the expansion cap.
    ExpansionCapExceeded,
    /// A store-wide aggregate byte limit was reached.
    TotalCapExceeded,
    /// Decompressed output of an entire block stream exceeded its aggregate cap.
    StreamOutputCapExceeded,

    // Compute (local environment)
    /// Filesystem or other local I/O failure.
    Io,
}

impl Code {
    /// Four-way category used for logging and HTTP mapping.
    pub fn category(self) -> Category {
        match self {
            Code::BadMagic
            | Code::BadVersion
            | Code::BadFrameType
            | Code::BadVarint
            | Code::BadLength
            | Code::BadDistance
            | Code::BadMatchLength
            | Code::BadTokenStream
            | Code::CrcMismatch
            | Code::LengthMismatch
            | Code::BadString
            | Code::BadRequest => Category::Input,
            Code::MissingPredecessor
            | Code::DigestMismatch
            | Code::IndexGap
            | Code::AlreadyExists
            | Code::NotFound => Category::State,
            Code::OutputCapExceeded
            | Code::PayloadCapExceeded
            | Code::ExpansionCapExceeded
            | Code::TotalCapExceeded
            | Code::StreamOutputCapExceeded => Category::Resource,
            Code::Io => Category::Compute,
        }
    }
}

/// The four error categories required to be distinguishable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Category {
    /// Malformed/untrusted input.
    Input,
    /// Sequencing/precondition conflict.
    State,
    /// Resource limit enforced.
    Resource,
    /// Local compute/I/O failure.
    Compute,
}

impl fmt::Display for Category {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Category::Input => "input",
            Category::State => "state",
            Category::Resource => "resource",
            Category::Compute => "compute",
        })
    }
}

/// Structured crate-wide error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub code: Code,
    /// Short human-readable judgement; no untrusted bytes echoed verbatim.
    pub detail: String,
    /// Populated only for [`Code::Io`].
    pub source: Option<Source>,
}

impl Error {
    pub fn new(code: Code, detail: impl Into<String>) -> Self {
        Error {
            code,
            detail: detail.into(),
            source: None,
        }
    }

    pub fn io(source: Source, detail: impl Into<String>) -> Self {
        Error {
            code: Code::Io,
            detail: detail.into(),
            source: Some(source),
        }
    }

    pub fn category(&self) -> Category {
        self.code.category()
    }

    /// Stable machine code, e.g. `bad_distance`.
    pub fn code_name(&self) -> &'static str {
        match self.code {
            Code::BadMagic => "bad_magic",
            Code::BadVersion => "bad_version",
            Code::BadFrameType => "bad_frame_type",
            Code::BadVarint => "bad_varint",
            Code::BadLength => "bad_length",
            Code::BadDistance => "bad_distance",
            Code::BadMatchLength => "bad_match_length",
            Code::BadTokenStream => "bad_token_stream",
            Code::CrcMismatch => "crc_mismatch",
            Code::LengthMismatch => "length_mismatch",
            Code::BadString => "bad_string",
            Code::BadRequest => "bad_request",
            Code::MissingPredecessor => "missing_predecessor",
            Code::DigestMismatch => "digest_mismatch",
            Code::IndexGap => "index_gap",
            Code::AlreadyExists => "already_exists",
            Code::NotFound => "not_found",
            Code::OutputCapExceeded => "output_cap_exceeded",
            Code::PayloadCapExceeded => "payload_cap_exceeded",
            Code::ExpansionCapExceeded => "expansion_cap_exceeded",
            Code::TotalCapExceeded => "total_cap_exceeded",
            Code::StreamOutputCapExceeded => "stream_output_cap_exceeded",
            Code::Io => "io",
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.source {
            Some(s) => write!(
                f,
                "[{}/{}] {} ({})",
                self.category(),
                self.code_name(),
                self.detail,
                s
            ),
            None => write!(
                f,
                "[{}/{}] {}",
                self.category(),
                self.code_name(),
                self.detail
            ),
        }
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn categories_are_stable() {
        assert_eq!(Code::BadDistance.category(), Category::Input);
        assert_eq!(Code::MissingPredecessor.category(), Category::State);
        assert_eq!(Code::OutputCapExceeded.category(), Category::Resource);
        assert_eq!(
            Error::io(Source::Store, "disk on fire").category(),
            Category::Compute
        );
        assert_eq!(Error::new(Code::BadMagic, "x").code_name(), "bad_magic");
    }
}
