//! Error taxonomy for the range-coder crate.
//!
//! Every fallible operation returns a [`std::error::Error`]-implementing enum
//! whose variant names describe *why* input was rejected.  Tests assert on
//! these variants (see `tests/`), so they are part of the public contract.

use std::fmt;

/// Errors caused by a frequency table itself, before any symbol is coded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TableError {
    /// `alphabet_size == 0` — there is no symbol that could be encoded.
    EmptyAlphabet,
    /// Every frequency entry is zero; at least one positive entry is needed.
    AllZeroFrequencies,
    /// `bound == 0` or `bound > 2**24`.  The 32-bit fixed-point kernel only
    /// guarantees non-overflow below `2**24`.
    InvalidBound { bound: u32 },
    /// Sum of frequencies exceeds the declared bound.  `point` is the first
    /// prefix-sum position where the bound was crossed.
    TotalExceeded {
        total: u32,
        bound: u32,
        point: usize,
    },
    /// A single frequency entry exceeds the bound by itself.
    EntryExceeded { index: usize, freq: u32, bound: u32 },
    /// `frequencies.len()` did not match the declared alphabet size.
    LengthMismatch { declared: usize, given: usize },
}

impl fmt::Display for TableError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TableError::EmptyAlphabet => write!(f, "alphabet must contain at least one symbol"),
            TableError::AllZeroFrequencies => {
                write!(
                    f,
                    "frequency table must contain at least one non-zero entry"
                )
            }
            TableError::InvalidBound { bound } => write!(
                f,
                "frequency bound {bound} is invalid (must be in 1..=2**24)"
            ),
            TableError::TotalExceeded {
                total,
                bound,
                point,
            } => write!(
                f,
                "frequency total {total} exceeds bound {bound} at symbol index {point}"
            ),
            TableError::EntryExceeded { index, freq, bound } => write!(
                f,
                "frequency entry {freq} at index {index} exceeds bound {bound}"
            ),
            TableError::LengthMismatch { declared, given } => write!(
                f,
                "frequency length mismatch: declared {declared}, given {given}"
            ),
        }
    }
}

impl std::error::Error for TableError {}

/// Errors raised by the raw range encoder.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EncodeError {
    /// Symbol index lies outside the table alphabet.
    SymbolOutOfRange { symbol: u32, alphabet: u32 },
    /// Symbol exists in the alphabet but its frequency is zero.  Per the
    /// integer contract, zero-probability symbols cannot be encoded.
    ZeroFrequency { symbol: u32 },
}

impl fmt::Display for EncodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EncodeError::SymbolOutOfRange { symbol, alphabet } => write!(
                f,
                "symbol {symbol} is outside the alphabet of size {alphabet}"
            ),
            EncodeError::ZeroFrequency { symbol } => {
                write!(
                    f,
                    "symbol {symbol} has zero frequency and cannot be encoded"
                )
            }
        }
    }
}

impl std::error::Error for EncodeError {}

/// Errors raised by the raw range decoder / stream finalization.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecodeError {
    /// Fewer bytes available than the kernel needed to read.
    TruncatedStream {
        needed: usize,
        available: usize,
        at: u64,
    },
    /// The first stream byte must be the seed `0x00`.
    InvalidLeadingByte { got: u8 },
    /// `code * total / range` landed on a cumulative slot `>= total`, i.e.
    /// the point lies in no symbol's interval.
    CodePointOutsideEnvelope { cum: u32, total: u32 },
    /// After exactly the declared number of symbols, one terminating zero
    /// byte must remain; it was missing or non-zero.
    BadTerminator { remaining: usize, got: Option<u8> },
    /// A chunk declared zero bytes of payload.
    EmptyPayload,
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DecodeError::TruncatedStream { needed, available, at } => write!(
                f,
                "range stream truncated: needed {needed} byte(s), had {available} (offset {at})"
            ),
            DecodeError::InvalidLeadingByte { got } => {
                write!(f, "range stream must start with seed byte 0x00, got 0x{got:02x}")
            }
            DecodeError::CodePointOutsideEnvelope { cum, total } => write!(
                f,
                "decoded code point {cum} lies outside the frequency envelope (total {total})"
            ),
            DecodeError::BadTerminator { remaining, got } => write!(
                f,
                "expected exactly one terminating zero byte, had {remaining} remaining (next byte {:?})",
                got.map(|b| format!("0x{b:02x}")).unwrap_or_else(|| "EOF".to_string())
            ),
            DecodeError::EmptyPayload => write!(f, "chunk payload must not be empty"),
        }
    }
}

impl std::error::Error for DecodeError {}

/// Errors in the framed, chunked container (header/segments/CRC/budgets).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContainerError {
    /// Magic bytes missing / wrong.
    BadMagic,
    /// Container format version is unsupported.
    UnsupportedVersion { version: u16 },
    /// Reserved/unknown flag bit set.
    UnknownFlags { flags: u16 },
    /// Header CRC32 did not match.
    HeaderCrcMismatch { declared: u32, computed: u32 },
    /// A segment frame's CRC32 did not match.
    FrameCrcMismatch {
        segment: u64,
        declared: u32,
        computed: u32,
    },
    /// A length field runs past the end of the file (truncated container).
    TruncatedContainer {
        segment: u64,
        needed: usize,
        available: usize,
    },
    /// Segment type byte unknown.
    UnknownSegmentType { segment: u64, marker: u8 },
    /// Container ended without the EOF segment.
    MissingEof,
    /// Bytes present after the EOF segment.
    TrailingBytesAfterEof { count: usize },
    /// The first segment is not `TABLE epoch=0`.
    MissingBaselineTable,
    /// Table epochs must be strictly increasing.
    BadEpoch { previous: u32, got: u32 },
    /// A chunk references an epoch no TABLE segment defined.
    UnknownEpoch { epoch: u32 },
    /// A CHUNK's symbol count is zero (zero-symbol chunks are not legal in
    /// the on-disk layout; chunk boundaries only happen between symbols).
    ZeroSymbolChunk,
    /// `sum(chunk symbols)` disagrees with the header/EOF declaration.
    SymbolCountMismatch { declared: u64, observed: u64 },
    /// Header/EOF disagree on the final symbol count.
    EofCountMismatch { header: u64, eof: u64 },
    /// Two chunks claimed the same sequence number.
    OutOfOrderChunks { previous_end: u64, chunk_start: u64 },
    /// A table embedded in the container was itself invalid.
    BadTable(TableError),
    /// Payload/table size fields overflowed the container limits.
    LengthOverflow { what: &'static str, value: u64 },
    /// Decoded symbol count would exceed the resource budget.
    BudgetExceeded { declared: u64, budget: u64 },
    /// The raw range stream inside a chunk was invalid.
    BadStream(DecodeError),
    /// Encoding refused a symbol (e.g. zero frequency) while writing.
    BadEncoding(EncodeError),
    /// Header field was internally inconsistent (bound, alphabet, …).
    BadHeader(&'static str),
}

impl fmt::Display for ContainerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ContainerError::BadMagic => write!(f, "bad container magic bytes"),
            ContainerError::UnsupportedVersion { version } => {
                write!(f, "unsupported container version {version}")
            }
            ContainerError::UnknownFlags { flags } => {
                write!(f, "unknown flag bits set: 0x{flags:04x}")
            }
            ContainerError::HeaderCrcMismatch { declared, computed } => write!(
                f,
                "header CRC mismatch: declared 0x{declared:08x}, computed 0x{computed:08x}"
            ),
            ContainerError::FrameCrcMismatch { segment, declared, computed } => write!(
                f,
                "segment {segment} CRC mismatch: declared 0x{declared:08x}, computed 0x{computed:08x}"
            ),
            ContainerError::TruncatedContainer { segment, needed, available } => write!(
                f,
                "truncated container at segment {segment}: needed {needed}, had {available}"
            ),
            ContainerError::UnknownSegmentType { segment, marker } => write!(
                f,
                "segment {segment} has unknown type marker 0x{marker:02x}"
            ),
            ContainerError::MissingEof => write!(f, "container is missing the EOF segment"),
            ContainerError::TrailingBytesAfterEof { count } => {
                write!(f, "{count} trailing byte(s) after EOF segment")
            }
            ContainerError::MissingBaselineTable => {
                write!(f, "first segment must be TABLE with epoch 0")
            }
            ContainerError::BadEpoch { previous, got } => write!(
                f,
                "table epochs must be strictly increasing (previous {previous}, got {got})"
            ),
            ContainerError::UnknownEpoch { epoch } => {
                write!(f, "chunk references unknown table epoch {epoch}")
            }
            ContainerError::ZeroSymbolChunk => write!(f, "chunk declares zero symbols"),
            ContainerError::SymbolCountMismatch { declared, observed } => write!(
                f,
                "symbol count mismatch: header declares {declared}, chunks contain {observed}"
            ),
            ContainerError::EofCountMismatch { header, eof } => write!(
                f,
                "EOF final symbol count {eof} disagrees with header {header}"
            ),
            ContainerError::OutOfOrderChunks { previous_end, chunk_start } => write!(
                f,
                "chunk starts at symbol {chunk_start} after previous chunk ended at {previous_end}"
            ),
            ContainerError::BadTable(e) => write!(f, "invalid frequency table: {e}"),
            ContainerError::LengthOverflow { what, value } => {
                write!(f, "length overflow for {what}: {value}")
            }
            ContainerError::BudgetExceeded { declared, budget } => write!(
                f,
                "declared symbol count {declared} exceeds resource budget {budget}"
            ),
            ContainerError::BadStream(e) => write!(f, "invalid range stream: {e}"),
            ContainerError::BadEncoding(e) => write!(f, "cannot encode symbol: {e}"),
            ContainerError::BadHeader(msg) => write!(f, "bad header: {msg}"),
        }
    }
}

impl std::error::Error for ContainerError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            ContainerError::BadTable(e) => Some(e),
            ContainerError::BadStream(e) => Some(e),
            ContainerError::BadEncoding(e) => Some(e),
            _ => None,
        }
    }
}

impl From<TableError> for ContainerError {
    fn from(e: TableError) -> Self {
        ContainerError::BadTable(e)
    }
}

impl From<DecodeError> for ContainerError {
    fn from(e: DecodeError) -> Self {
        ContainerError::BadStream(e)
    }
}

impl From<EncodeError> for ContainerError {
    fn from(e: EncodeError) -> Self {
        ContainerError::BadEncoding(e)
    }
}
