//! Decode failures with a specific category, so tests can assert the exact
//! failure class rather than only "something went wrong".

use core::fmt;

/// Every way a malformed or corrupted file is rejected.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FormatError {
    /// File shorter than the fixed header, or a declared region runs past
    /// the end of the buffer.
    Truncated,
    /// Magic bytes are not `HBS1`.
    BadMagic,
    /// Version is not supported.
    UnsupportedVersion {
        /// Version found in the file.
        found: u16,
    },
    /// Reserved flags are not zero.
    BadFlags(u16),
    /// Header-level lengths or offsets contradict one another.
    BadHeader(&'static str),
    /// Checksum does not match the payload.
    ChecksumMismatch {
        /// CRC stored in the file.
        stored: u32,
        /// CRC computed over the bytes.
        computed: u32,
    },
    /// A directory entry is structurally invalid.
    BadDirectory(&'static str),
    /// Container kind tag is neither array (1) nor bitmap (2).
    UnknownContainerKind(u16),
    /// Declared cardinality violates the representation's bounds, or does
    /// not match the content.
    Cardinality {
        /// Chunk whose container failed validation.
        chunk: u16,
        /// Detail.
        detail: CardinalityError,
    },
    /// Array values are not strictly increasing.
    ArrayNotSorted {
        /// Chunk whose container failed validation.
        chunk: u16,
    },
    /// Trailing bytes exist after the declared data region.
    TrailingBytes,
}

/// Cardinality-specific failures (the contract: serialization validates
/// cardinality and offsets).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CardinalityError {
    /// Declared size does not match the decoded element count.
    Mismatch {
        /// Cardinality stored in the directory.
        declared: usize,
        /// Cardinality derived from the payload.
        actual: usize,
    },
    /// Array container claims more values than the sparse threshold.
    ArrayTooDense {
        /// Cardinality stored in the directory.
        declared: usize,
    },
    /// Bitmap container claims at most the sparse threshold.
    BitmapTooSparse {
        /// Cardinality stored in the directory.
        declared: usize,
    },
    /// Declared zero elements; empty chunks must not be serialised.
    Empty,
}

impl fmt::Display for FormatError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FormatError::Truncated => {
                f.write_str("file truncated: a declared region exceeds the buffer")
            }
            FormatError::BadMagic => f.write_str("bad magic: expected HBS1"),
            FormatError::UnsupportedVersion { found } => {
                write!(f, "unsupported format version: {found}")
            }
            FormatError::BadFlags(fl) => write!(f, "reserved flags must be zero, found {fl:#06x}"),
            FormatError::BadHeader(msg) => write!(f, "inconsistent header: {msg}"),
            FormatError::ChecksumMismatch { stored, computed } => write!(
                f,
                "checksum mismatch: stored {stored:#010x}, computed {computed:#010x}"
            ),
            FormatError::BadDirectory(msg) => write!(f, "invalid directory: {msg}"),
            FormatError::UnknownContainerKind(k) => write!(f, "unknown container kind tag: {k}"),
            FormatError::Cardinality { chunk, detail } => {
                write!(f, "cardinality invalid in chunk {chunk}: {detail}")
            }
            FormatError::ArrayNotSorted { chunk } => {
                write!(
                    f,
                    "array payload in chunk {chunk} is not strictly increasing"
                )
            }
            FormatError::TrailingBytes => f.write_str("trailing bytes after declared data region"),
        }
    }
}

impl fmt::Display for CardinalityError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CardinalityError::Mismatch { declared, actual } => {
                write!(f, "declared {declared}, actual {actual}")
            }
            CardinalityError::ArrayTooDense { declared } => write!(
                f,
                "array declares {declared} elements, above the sparse threshold"
            ),
            CardinalityError::BitmapTooSparse { declared } => write!(
                f,
                "bitmap declares {declared} elements, at or below sparse threshold"
            ),
            CardinalityError::Empty => {
                f.write_str("container is empty; empty chunks are not serialised")
            }
        }
    }
}

impl std::error::Error for FormatError {}
