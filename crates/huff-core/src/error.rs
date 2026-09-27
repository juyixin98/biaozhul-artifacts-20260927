//! Typed errors with stable machine codes (see FORMAT.md §9).
//!
//! Every decode/validation failure maps to exactly one [`HuffError`] variant;
//! unknown states are never reported as success.

use std::fmt;

/// All failure kinds produced by the codec.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HuffError {
    /// Container magic bytes are not `HUFF`.
    BadMagic,
    /// Version byte is not a version this build understands (`1`).
    UnknownVersion,
    /// Claimed header extends past the end of the file.
    HeaderTruncated,
    /// Header CRC-32 does not match the header bytes.
    HeaderCrcMismatch,
    /// Claimed block frame extends past the end of the file.
    BlockFrameTruncated,
    /// Block frame CRC-32 does not match its payload.
    BlockCrcMismatch,
    /// Claimed directory entry / footer extends past the end of the file.
    DirectoryTruncated,
    /// Directory CRC-32 does not match the directory bytes.
    DirectoryCrcMismatch,
    /// Directory offset/length do not agree with the header/footer layout.
    DirectoryBoundsInvalid,
    /// Directory entries are not strictly ascending and contiguous.
    DirectoryNotContiguous,
    /// Header `original_total` disagrees with the sum of block lengths.
    TotalLengthMismatch,
    /// File has trailing bytes after the footer.
    TrailingData,
    /// Declared block size is 0 or exceeds [`crate::MAX_BLOCK_SIZE`].
    BadBlockSize,
    /// Payload header is shorter than its fixed fields.
    PayloadTruncated,
    /// Stored payload length does not match the bytes actually present.
    PayloadLengthMismatch,
    /// A code length exceeds [`crate::MAX_CODE_LEN`].
    CodeLenTooLong,
    /// More than 255 symbols are declared.
    BadSymbolCount,
    /// A present symbol has code length 0 (or vice versa).
    TableSymbolShapeInvalid,
    /// Kraft sum of the code lengths is greater than 1.
    TableKraftOverflow,
    /// Non-empty table whose Kraft sum is exactly 1 requires ≥ 2 symbols;
    /// a one-symbol alphabet must use the length-0 convention.
    TableIncomplete,
    /// `bits_total` declares bits past the payload body.
    BitstreamTruncated,
    /// The bit reader ran out of bits while reading a codeword.
    TruncatedCodeword,
    /// No canonical code exists at the observed prefix.
    InvalidCodeword,
    /// Padding bits after the final codeword are not all zero.
    InvalidPadding,
    /// Decoder emitted a different byte count than `original_len`.
    OutputLengthMismatch,
    /// Decoder was asked to emit more than `original_len` bytes.
    OutputTooLong,
    /// Reserved header flags contain unknown bits.
    UnknownFlags,
}

impl HuffError {
    /// Stable machine code (kept stable across releases; used by API and tests).
    pub fn code(self) -> &'static str {
        match self {
            HuffError::BadMagic => "BAD_MAGIC",
            HuffError::UnknownVersion => "UNKNOWN_VERSION",
            HuffError::HeaderTruncated => "HEADER_TRUNCATED",
            HuffError::HeaderCrcMismatch => "HEADER_CRC_MISMATCH",
            HuffError::BlockFrameTruncated => "BLOCK_FRAME_TRUNCATED",
            HuffError::BlockCrcMismatch => "BLOCK_CRC_MISMATCH",
            HuffError::DirectoryTruncated => "DIRECTORY_TRUNCATED",
            HuffError::DirectoryCrcMismatch => "DIRECTORY_CRC_MISMATCH",
            HuffError::DirectoryBoundsInvalid => "DIRECTORY_BOUNDS_INVALID",
            HuffError::DirectoryNotContiguous => "DIRECTORY_NOT_CONTIGUOUS",
            HuffError::TotalLengthMismatch => "TOTAL_LENGTH_MISMATCH",
            HuffError::TrailingData => "TRAILING_DATA",
            HuffError::BadBlockSize => "BAD_BLOCK_SIZE",
            HuffError::PayloadTruncated => "BLOCK_PAYLOAD_TRUNCATED",
            HuffError::PayloadLengthMismatch => "BLOCK_PAYLOAD_LENGTH_MISMATCH",
            HuffError::CodeLenTooLong => "CODE_LEN_TOO_LONG",
            HuffError::BadSymbolCount => "BAD_SYMBOL_COUNT",
            HuffError::TableSymbolShapeInvalid => "TABLE_SYMBOL_SHAPE_INVALID",
            HuffError::TableKraftOverflow => "TABLE_KRAFT_OVERFLOW",
            HuffError::TableIncomplete => "TABLE_INCOMPLETE",
            HuffError::BitstreamTruncated => "BITSTREAM_TRUNCATED",
            HuffError::TruncatedCodeword => "TRUNCATED_CODEWORD",
            HuffError::InvalidCodeword => "INVALID_CODEWORD",
            HuffError::InvalidPadding => "INVALID_PADDING",
            HuffError::OutputLengthMismatch => "OUTPUT_LENGTH_MISMATCH",
            HuffError::OutputTooLong => "OUTPUT_TOO_LONG",
            HuffError::UnknownFlags => "UNKNOWN_FLAGS",
        }
    }
}

impl fmt::Display for HuffError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.code())
    }
}

impl std::error::Error for HuffError {}

/// Convenience alias used throughout the kernel.
pub type Result<T> = std::result::Result<T, HuffError>;
