//! Error taxonomy for the range codec.
//!
//! Every failure that can be observed through the library, the CLI or the HTTP
//! API maps to a [`CodecError`] variant carrying a [`Decision`]: whether the
//! input was **rejected** (malformed and safely ignorable) or is
//! **indeterminate** (well-formed framing, but this build cannot interpret it,
//! e.g. an unknown reserved flag).
//!
//! Numeric codes are stable and appear verbatim in diagnostics and in the
//! HTTP `error.code` field, so callers can match on them without parsing text.

use thiserror::Error;

/// High-level disposition of a failed validation/decode attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Decision {
    /// The input is definitely invalid.
    Rejected,
    /// Framing is valid but meaning cannot be established by this build.
    Indeterminate,
}

impl Decision {
    pub fn as_str(self) -> &'static str {
        match self {
            Decision::Rejected => "rejected",
            Decision::Indeterminate => "indeterminate",
        }
    }
}

/// All errors produced by the codec. See module docs for the decision model.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum CodecError {
    // ---- frequency-table validation ------------------------------------
    /// A symbol with zero frequency was asked to be encoded.
    #[error("symbol {symbol} has zero frequency and cannot be encoded")]
    ZeroFrequencySymbol { symbol: u32 },

    /// Referenced symbol is outside `[0, num_symbols)`.
    #[error("symbol {symbol} is out of range (alphabet size {num_symbols})")]
    SymbolOutOfRange { symbol: u32, num_symbols: u32 },

    /// Frequency table contained no symbols, or more than
    /// [`crate::model::MAX_SYMBOLS`].
    #[error("bad alphabet size {size}: must be in 1..={max}")]
    BadAlphabetSize { size: usize, max: u32 },

    /// Sum of frequencies exceeds [`crate::model::MAX_FREQ_TOTAL`] or is zero.
    #[error("frequency total {total} is out of bounds (1..={max})")]
    FrequencyTotalOutOfBounds { total: u64, max: u32 },

    // ---- stream framing -----------------------------------------------
    /// Fewer bytes available than the format requires. `needed` may be
    /// `None` when the length is itself encoded later in the stream.
    #[error("truncated stream: needed at least {needed} more bytes, had {available}")]
    Truncated { needed: usize, available: usize },

    /// Input had bytes left over after a complete decode.
    #[error("trailing {count} byte(s) after end marker")]
    TrailingBytes { count: usize },

    /// Magic bytes did not match `RC01`.
    #[error("bad magic {found:02X?}, expected [52, 43, 30, 31]")]
    BadMagic { found: [u8; 4] },

    /// Container version is not supported.
    #[error("unsupported container version {found}")]
    UnsupportedVersion { found: u8 },

    /// Reserved header flag is set. The container may be valid for a newer
    /// build, so this is [`Decision::Indeterminate`].
    #[error("reserved flag 0x{flag:02X} is set; this build cannot interpret it")]
    ReservedFlag { flag: u8 },

    /// Declared length exceeds the configured resource budget.
    #[error("declared length {declared} exceeds budget {budget}")]
    LengthBudgetExceeded { declared: u64, budget: u64 },

    /// Declared length exceeds the hard container limit.
    #[error("declared length {declared} exceeds hard limit {limit}")]
    LengthTooLarge { declared: u64, limit: u64 },

    /// Actual decoded symbol count differs from the declared length.
    #[error("length mismatch: declared {declared}, decoded {decoded}")]
    LengthMismatch { declared: u64, decoded: u64 },

    /// Per-chunk or whole-message byte budget was exceeded while decoding.
    #[error("byte budget exhausted: used {used}, budget {budget}")]
    ByteBudgetExceeded { used: usize, budget: usize },

    /// More chunks than the configured budget.
    #[error("chunk budget exceeded: {chunks} > {budget}")]
    ChunkBudgetExceeded { chunks: usize, budget: usize },

    /// Unknown chunk type tag.
    #[error("unknown chunk type 0x{tag:02X}")]
    UnknownChunkType { tag: u8 },

    /// CRC32 of a chunk payload did not match the stored checksum.
    #[error("crc mismatch in chunk {index}: stored {stored:08X}, computed {computed:08X}")]
    CrcMismatch {
        index: usize,
        stored: u32,
        computed: u32,
    },

    /// A chunk symbol count was zero or larger than permitted.
    #[error("bad chunk length {length} at chunk {index}")]
    BadChunkLength { index: usize, length: u32 },

    // ---- range-coder kernel -------------------------------------------
    /// The normalised code value fell outside every cumulative interval;
    /// only possible on corrupt/hostile streams.
    #[error("code value {code} outside total interval {total} (corrupt stream)")]
    CodeOutsideRange { code: u32, total: u32 },

    /// The kernel's initial byte (the carry sentinel) was not `0` or `1`.
    #[error("invalid range-coder init byte {found:#04X}, expected 0x00 or 0x01")]
    InvalidInitByte { found: u8 },

    /// Carry reached past the protected sentinel position. This is an
    /// internal invariant failure, not a data error.
    #[error("carry overflow past sentinel (internal invariant failure)")]
    CarryOverflow,

    /// Decoder was asked for more symbols after the stream's declared end.
    #[error("decoder exhausted")]
    DecoderExhausted,

    /// Encoder was asked to encode a stream longer than it was sized for
    /// (output-buffer guard).
    #[error("encoder output exceeds {limit} bytes")]
    OutputLimitExceeded { limit: usize },

    // ---- persistence / io ---------------------------------------------
    /// Filesystem error with a redacted path context.
    #[error("io error ({context}): {message}")]
    Io { context: String, message: String },

    /// Persisted artifact failed validation on load.
    #[error("stored artifact rejected: {0}")]
    StoredArtifact(String),

    /// Requested artifact id was not found.
    #[error("artifact not found: {0}")]
    NotFound(String),

    /// HTTP request body was rejected before any codec work began.
    #[error("bad request: {0}")]
    BadRequest(String),

    /// Payload exceeded the HTTP body-size limit.
    #[error("payload too large ({size} > {limit} bytes)")]
    PayloadTooLarge { size: usize, limit: usize },

    /// Content type was not JSON where JSON is required.
    #[error("unsupported content type")]
    UnsupportedMediaType,
}

impl CodecError {
    /// Whether the error means "definitely bad" or "cannot judge".
    pub fn decision(&self) -> Decision {
        match self {
            CodecError::ReservedFlag { .. } => Decision::Indeterminate,
            _ => Decision::Rejected,
        }
    }

    /// Stable short code used in API responses.
    pub fn code(&self) -> &'static str {
        match self {
            CodecError::ZeroFrequencySymbol { .. } => "ZERO_FREQUENCY_SYMBOL",
            CodecError::SymbolOutOfRange { .. } => "SYMBOL_OUT_OF_RANGE",
            CodecError::BadAlphabetSize { .. } => "BAD_ALPHABET_SIZE",
            CodecError::FrequencyTotalOutOfBounds { .. } => "FREQ_TOTAL_OUT_OF_BOUNDS",
            CodecError::Truncated { .. } => "TRUNCATED",
            CodecError::TrailingBytes { .. } => "TRAILING_BYTES",
            CodecError::BadMagic { .. } => "BAD_MAGIC",
            CodecError::UnsupportedVersion { .. } => "UNSUPPORTED_VERSION",
            CodecError::ReservedFlag { .. } => "RESERVED_FLAG",
            CodecError::LengthBudgetExceeded { .. } => "LENGTH_BUDGET_EXCEEDED",
            CodecError::LengthTooLarge { .. } => "LENGTH_TOO_LARGE",
            CodecError::LengthMismatch { .. } => "LENGTH_MISMATCH",
            CodecError::ByteBudgetExceeded { .. } => "BYTE_BUDGET_EXCEEDED",
            CodecError::ChunkBudgetExceeded { .. } => "CHUNK_BUDGET_EXCEEDED",
            CodecError::UnknownChunkType { .. } => "UNKNOWN_CHUNK_TYPE",
            CodecError::CrcMismatch { .. } => "CRC_MISMATCH",
            CodecError::BadChunkLength { .. } => "BAD_CHUNK_LENGTH",
            CodecError::CodeOutsideRange { .. } => "CODE_OUTSIDE_RANGE",
            CodecError::InvalidInitByte { .. } => "INVALID_INIT_BYTE",
            CodecError::CarryOverflow => "CARRY_OVERFLOW",
            CodecError::DecoderExhausted => "DECODER_EXHAUSTED",
            CodecError::OutputLimitExceeded { .. } => "OUTPUT_LIMIT_EXCEEDED",
            CodecError::Io { .. } => "IO_ERROR",
            CodecError::StoredArtifact(_) => "STORED_ARTIFACT",
            CodecError::NotFound(_) => "NOT_FOUND",
            CodecError::BadRequest(_) => "BAD_REQUEST",
            CodecError::PayloadTooLarge { .. } => "PAYLOAD_TOO_LARGE",
            CodecError::UnsupportedMediaType => "UNSUPPORTED_MEDIA_TYPE",
        }
    }

    /// HTTP status mapping used by the Axum layer.
    pub fn http_status(&self) -> u16 {
        match self {
            CodecError::NotFound(_) => 404,
            CodecError::UnsupportedMediaType => 415,
            CodecError::PayloadTooLarge { .. }
            | CodecError::LengthBudgetExceeded { .. }
            | CodecError::LengthTooLarge { .. }
            | CodecError::ByteBudgetExceeded { .. }
            | CodecError::ChunkBudgetExceeded { .. }
            | CodecError::OutputLimitExceeded { .. } => 413,
            CodecError::BadRequest(_)
            | CodecError::ZeroFrequencySymbol { .. }
            | CodecError::SymbolOutOfRange { .. }
            | CodecError::BadAlphabetSize { .. }
            | CodecError::FrequencyTotalOutOfBounds { .. }
            | CodecError::Truncated { .. }
            | CodecError::TrailingBytes { .. }
            | CodecError::BadMagic { .. }
            | CodecError::LengthMismatch { .. }
            | CodecError::UnknownChunkType { .. }
            | CodecError::CrcMismatch { .. }
            | CodecError::BadChunkLength { .. }
            | CodecError::CodeOutsideRange { .. }
            | CodecError::InvalidInitByte { .. }
            | CodecError::DecoderExhausted
            | CodecError::StoredArtifact(_) => 422,
            CodecError::UnsupportedVersion { .. } | CodecError::ReservedFlag { .. } => 422,
            CodecError::CarryOverflow => 500,
            CodecError::Io { .. } => 500,
        }
    }
}

impl From<std::io::Error> for CodecError {
    fn from(e: std::io::Error) -> Self {
        CodecError::Io {
            context: "io".into(),
            message: e.to_string(),
        }
    }
}

pub type Result<T> = std::result::Result<T, CodecError>;
