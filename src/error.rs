//! Error taxonomy shared by the codec kernel, container, persistence and API layers.
//!
//! Every failure mode that is reachable when decoding untrusted data gets its own
//! stable variant. Tests assert on the *category* (see [`Error::kind`]) rather than
//! on wording, so the human-readable messages below are free to change.

use std::fmt;

/// Stable, machine-readable failure categories.
///
/// The string returned by [`ErrorKind::as_str`] is part of the API contract and is
/// also used verbatim as the JSON `error` field on HTTP responses; do not rename.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// Input block longer than the configured maximum.
    BlockTooLarge,
    /// A canonical code length exceeds the format limit (32 bits).
    CodeLengthTooLong,
    /// The code-length set does not satisfy Kraft equality: `sum 2^-len != 1`.
    OverSubscribedTree,
    /// RLE run in the code-length table would run past the end of the table.
    InvalidRleRun,
    /// Reserved RLE opcode encountered.
    ReservedRleOpcode,
    /// Code-length table contains duplicate / unsorted symbols.
    DuplicateSymbol,
    /// Multi-symbol table assigns a zero length to a symbol (only a lone symbol may).
    ZeroLengthInMultiSymbolTable,
    /// A single-symbol table does not use the reserved zero length.
    InvalidSingleSymbolCode,
    /// Block header claims more symbols than the alphabet allows (256).
    TooManySymbols,
    /// Block payload is shorter than the block header requires.
    TruncatedBlock,
    /// A code word was started but the bitstream ended before it completed.
    TruncatedBitstream,
    /// Padding bits after the final code word are not all zero.
    InvalidTrailingBits,
    /// Read bits do not correspond to any assigned canonical code.
    UnknownCode,
    /// Number of decoded symbols does not match the stored original length.
    LengthMismatch,
    /// The empty-input symbol does not decode to the expected number of output bytes.
    SingleSymbolLengthMismatch,
    /// CRC32 of a block payload (bytes at rest) does not match the directory.
    PayloadCrcMismatch,
    /// CRC32 of the decoded original bytes does not match the directory.
    OriginalCrcMismatch,
    /// Container magic bytes are wrong (not `HCMP`).
    BadMagic,
    /// Container format version is not supported by this implementation.
    UnknownVersion,
    /// Reserved header flags are set.
    BadFlags,
    /// Container is smaller than the fixed 16-byte global header.
    TruncatedHeader,
    /// Directory runs past the end of the file.
    TruncatedDirectory,
    /// CRC32 of the block directory does not match the header.
    DirectoryCrcMismatch,
    /// Declared block count exceeds the hard safety limit.
    TooManyBlocks,
    /// Two directory entries carry the same block id.
    DuplicateBlockId,
    /// Directory offsets overlap or are not strictly increasing.
    PayloadOverlap,
    /// A payload offset/length points outside the file.
    PayloadOutOfBounds,
    /// File has bytes after the last payload.
    TrailingGarbage,
    /// Requested object id was not found in the store.
    NotFound,
    /// Object id is not a safe, canonical filesystem-relative identifier.
    InvalidId,
    /// Object already exists and the call did not allow overwriting.
    AlreadyExists,
    /// Underlying filesystem / IO failure.
    Io,
}

impl ErrorKind {
    /// Wire-stable identifier of this category.
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorKind::BlockTooLarge => "block_too_large",
            ErrorKind::CodeLengthTooLong => "code_length_too_long",
            ErrorKind::OverSubscribedTree => "over_subscribed_tree",
            ErrorKind::InvalidRleRun => "invalid_rle_run",
            ErrorKind::ReservedRleOpcode => "reserved_rle_opcode",
            ErrorKind::DuplicateSymbol => "duplicate_symbol",
            ErrorKind::ZeroLengthInMultiSymbolTable => "zero_length_in_multi_symbol_table",
            ErrorKind::InvalidSingleSymbolCode => "invalid_single_symbol_code",
            ErrorKind::TooManySymbols => "too_many_symbols",
            ErrorKind::TruncatedBlock => "truncated_block",
            ErrorKind::TruncatedBitstream => "truncated_bitstream",
            ErrorKind::InvalidTrailingBits => "invalid_trailing_bits",
            ErrorKind::UnknownCode => "unknown_code",
            ErrorKind::LengthMismatch => "length_mismatch",
            ErrorKind::SingleSymbolLengthMismatch => "single_symbol_length_mismatch",
            ErrorKind::PayloadCrcMismatch => "payload_crc_mismatch",
            ErrorKind::OriginalCrcMismatch => "original_crc_mismatch",
            ErrorKind::BadMagic => "bad_magic",
            ErrorKind::UnknownVersion => "unknown_version",
            ErrorKind::BadFlags => "bad_flags",
            ErrorKind::TruncatedHeader => "truncated_header",
            ErrorKind::TruncatedDirectory => "truncated_directory",
            ErrorKind::DirectoryCrcMismatch => "directory_crc_mismatch",
            ErrorKind::TooManyBlocks => "too_many_blocks",
            ErrorKind::DuplicateBlockId => "duplicate_block_id",
            ErrorKind::PayloadOverlap => "payload_overlap",
            ErrorKind::PayloadOutOfBounds => "payload_out_of_bounds",
            ErrorKind::TrailingGarbage => "trailing_garbage",
            ErrorKind::NotFound => "not_found",
            ErrorKind::InvalidId => "invalid_id",
            ErrorKind::AlreadyExists => "already_exists",
            ErrorKind::Io => "io_error",
        }
    }

    /// Default HTTP status used when this category surfaces through the API.
    pub fn http_status(self) -> u16 {
        match self {
            ErrorKind::NotFound => 404,
            ErrorKind::AlreadyExists => 409,
            ErrorKind::InvalidId | ErrorKind::BlockTooLarge => 400,
            ErrorKind::Io => 500,
            // Every codec/container rejection means the bytes (or body) are
            // malformed; the client supplied them, so it is a 422.
            _ => 422,
        }
    }
}

impl fmt::Display for ErrorKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// The single error type used throughout the crate.
#[derive(Debug)]
pub struct Error {
    kind: ErrorKind,
    context: String,
}

impl Error {
    /// Construct an error of `kind` with an explanatory `context` message.
    pub fn new(kind: ErrorKind, context: impl Into<String>) -> Self {
        Error {
            kind,
            context: context.into(),
        }
    }

    /// Stable failure category.
    pub fn kind(&self) -> ErrorKind {
        self.kind
    }

    /// Human-readable detail (not part of the stable contract).
    pub fn context(&self) -> &str {
        &self.context
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.kind.as_str(), self.context)
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        match e.kind() {
            std::io::ErrorKind::NotFound => Error::new(ErrorKind::NotFound, e.to_string()),
            std::io::ErrorKind::AlreadyExists => {
                Error::new(ErrorKind::AlreadyExists, e.to_string())
            }
            _ => Error::new(ErrorKind::Io, e.to_string()),
        }
    }
}

/// Convenience alias used by every fallible API in the crate.
pub type Result<T> = std::result::Result<T, Error>;
