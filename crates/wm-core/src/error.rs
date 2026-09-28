//! Kernel-level error contract.
//!
//! Every rejection carries a stable machine-readable [`WmError::kind`]
//! string so that the HTTP layer, tests and logs can agree on failure
//! categories without parsing prose.

use thiserror::Error;

/// Errors raised by wavelet matrix construction and queries.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum WmError {
    /// Build input was empty; an index must contain at least one element.
    #[error("cannot build index from an empty value sequence")]
    EmptyValues,

    /// Query range `[l, r)` violates `0 <= l <= r <= n`.
    #[error("range [{l}, {r}) is out of bounds for sequence length {n}")]
    RangeOutOfBounds { l: usize, r: usize, n: usize },

    /// Query range `[l, r)` is empty (`l == r`).
    #[error("range [{l}, {r}) is empty; queries require a non-empty half-open range")]
    EmptyRange { l: usize, r: usize },

    /// k-th query used `k >= r - l` (k is 0-based).
    #[error("k = {k} is out of bounds for range [{l}, {r}) of length {len}; k is 0-based")]
    KOutOfBounds {
        k: u64,
        l: usize,
        r: usize,
        len: usize,
    },
}

impl WmError {
    /// Stable snake_case category identifier.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            WmError::EmptyValues => "EMPTY_VALUES",
            WmError::RangeOutOfBounds { .. } => "RANGE_OUT_OF_BOUNDS",
            WmError::EmptyRange { .. } => "EMPTY_RANGE",
            WmError::KOutOfBounds { .. } => "K_OUT_OF_BOUNDS",
        }
    }
}
